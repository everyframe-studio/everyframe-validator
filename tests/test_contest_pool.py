import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from everyframe_validator import assignment, assignment_publisher, cadence
from everyframe_validator.core import contest_pool as pool
from everyframe_validator.core.chain_scope import scope
from everyframe_validator.core.validator import epoch_weight_plan
from everyframe_validator.core.reconcile_weights import observation_matches
from everyframe_validator.feed import verify
from everyframe_validator.publish import sign
from everyframe_validator.state import atomic_json, journal_rows
from conftest import hot
from test_scheduled import scheduled_case, run
from test_burn_selection import rotation, publisher_case, owner_neurons


@pytest.fixture
def contest(rotation, signing, monkeypatch):
    import bittensor as bt
    case, raw, _, _, _, Base = rotation
    ns = owner_neurons() + [{'uid': 6, 'hotkey': hot(6), 'coldkey': hot(16), 'validator_permit': False}]
    case[2]['neurons'] = ns
    class Client(Base):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            prior = self.query
            async def query(storage, args, block=None):
                if storage[1] == 'Owner' and args[0] == hot(6): return hot(16)
                return await prior(storage, args, block=block)
            self.query = query
            async def graph(netuid, block=None, commitments=False):
                return SimpleNamespace(block=block, neurons=[SimpleNamespace(**n) for n in ns])
            self.subnets.metagraph = graph
    monkeypatch.setattr(bt, 'Client', Client)
    report = verify(raw, signing[1], scope('test'), 10)
    base = assignment.project(scope('test'), case[2], case[3], report)
    selected = asyncio.run(pool.from_chain(Client(), base, {'hotkey': hot(6), 'coldkey': hot(16)}))
    assigned = assignment.project(scope('test'), case[2], case[3], report, contest_pool=selected)
    envelope = json.dumps(sign(assigned, signing[0])).encode()
    monkeypatch.setattr(assignment, 'fetch', lambda url, epoch: envelope if '/allocations/' in url else raw)
    pool.accept(case[0], {'hotkey': hot(6), 'coldkey': hot(16)})
    return case, assigned, report, Client


def test_only_remainder_changes_serving_weights_stay_identical(contest):
    case, selected, report, _ = contest
    args = (report['artifact'], report['bindings'], case[2]['neurons'], hot(1), case[2]['params'], hot(10), hot(0))
    old = epoch_weight_plan(*args)
    new = epoch_weight_plan(*args, contest_pool=selected['contestPool'])
    assert new['policy'] == pool.WEIGHT_POLICY
    assert new['weights'][6] == old['weights'][0]
    for uid in (2, 3): assert new['weights'][uid] == old['weights'][uid]
    assert sum(new['weights'].values()) == 65535
    assert new['contestPoolBudgetUnits'] == old['burnBudgetUnits']
    assert 'burnUid' not in new and not new['allBurn']
    assert new['totalWorkMicrousd'] == old['totalWorkMicrousd']


def test_full_work_pool_funds_no_contest(contest):
    case, selected, report, _ = contest
    a = deepcopy(report['artifact'])
    for score in a['window']['scores']: score['earningsMicrousd'] *= 100
    new = epoch_weight_plan(a, report['bindings'], case[2]['neurons'], hot(1), case[2]['params'], hot(10), hot(0), contest_pool=selected['contestPool'])
    assert new['contestPoolBudgetUnits'] == 0 and 6 not in new['weights']


def test_zero_work_is_explicit_pool_allocation(contest):
    case, selected, _, _ = contest
    plan = cadence.burn_plan(case[2]['neurons'], hot(1), case[2]['params'], hot(10), hot(0), contest_pool=selected['contestPool'])
    assert plan['weights'] == {6: 65535}
    assert plan['allContestPool'] and not plan['allBurn']


def test_running_validator_accepts_and_never_pays_twice(contest, chain):
    case, selected, _, _ = contest
    assert run(case)['state'] == 'commit_finalized'
    saved = journal_rows(case[0])[0]['record']
    assert saved['plan']['contestPool'] == selected['contestPool']
    assert saved['assignmentPolicy'] == assignment.CONTEST_POLICY
    assert run(case)['reason'] == 'chain_epoch_already_committed'
    assert chain.execute.await_count == 1


def test_missing_consent_cannot_sign(contest, chain):
    case = contest[0]; (case[0] / 'contest-pool-consent.json').unlink()
    with pytest.raises(ValueError, match='acceptance_required'): run(case)
    chain.execute.assert_not_called()
    assert journal_rows(case[0]) == []


def test_no_downgrade_after_acceptance(contest):
    case, selected, _, Client = contest
    asyncio.run(pool.verify(Client(), selected, case[0], persist=True))
    old = {k: v for k, v in selected.items() if k != 'contestPool'}
    with pytest.raises(ValueError, match='downgrade'): asyncio.run(pool.verify(Client(), old, case[0]))
    old['chainEpoch'] -= 1
    asyncio.run(pool.verify(Client(), old, case[0]))


@pytest.mark.parametrize('field,value', [('uid', True), ('coldkey', hot(10)), ('snapshotBlock', 9), ('hotkey', 'invalid')])
def test_invalid_signed_pool_rejected(contest, signing, field, value):
    case, assigned, _, _ = contest
    bad = deepcopy(assigned); bad['contestPool'][field] = value
    with pytest.raises(ValueError):
        assignment.authenticate(json.dumps(sign(bad, signing[0])), signing[1], scope('test'), case[2], case[3])


@pytest.mark.parametrize('field,value', [('uid', 7), ('hotkey', hot(7)), ('coldkey', hot(17))])
def test_identity_changes_stop_signing(contest, field, value):
    case, assigned, _, _ = contest
    ns = deepcopy(case[2]['neurons']); ns[-1][field] = value
    with pytest.raises(ValueError, match='identity_changed'):
        pool.resolve(ns, hot(10), hot(0), assigned['contestPool'])


def test_pool_cannot_claim_miner_work(contest):
    case, assigned, report, _ = contest
    bindings = {**report['bindings'], report['artifact']['window']['scores'][0]['minerId']: hot(6)}
    with pytest.raises(ValueError, match='cannot_earn_serving'):
        epoch_weight_plan(report['artifact'], bindings, case[2]['neurons'], hot(1), case[2]['params'], hot(10), hot(0), contest_pool=assigned['contestPool'])


def test_chain_check_detects_forgery_even_with_valid_signature(contest):
    case, assigned, _, Client = contest
    bad = deepcopy(assigned); bad['contestPool']['uid'] = 7
    with pytest.raises(ValueError, match='does_not_match_chain'):
        asyncio.run(pool.verify(Client(), bad, case[0]))


def test_reuse_window_has_no_second_miner_payment(contest, signing, monkeypatch):
    case, assigned, _, _ = contest
    case[3].update(LastEpochBlock=3970, previousChainEpochStart=3610, chainEpochStartHash='0x'+'b'*64)
    selected = {**assigned['contestPool'], 'snapshotBlock': 3970, 'snapshotBlockHash': '0x'+'b'*64}
    p = assignment.project(scope('test'), case[2], case[3], contest_pool=selected)
    raw = json.dumps(sign(p, signing[0])).encode()
    monkeypatch.setattr(assignment, 'fetch', lambda url, epoch: raw if '/allocations/' in url else pytest.fail('no report expected'))
    result = run(case, submit=False)
    assert result['accountingSource'] == 'scheduled_reuse_pool'
    assert result['weights'] == {6: 65535}
    assert result['allContestPool']


def test_future_activation_pinned_before_publication(publisher_case, contest, monkeypatch):
    args = publisher_case
    args.contest_pool_from_chain_epoch = 21
    args.contest_pool_hotkey = hot(6); args.contest_pool_coldkey = hot(16)
    original = assignment_publisher.atomic_json
    def fail(path, *a, **kw):
        if path.name == '20.json':
            assert (path.parent / 'contest-pool-activation.json').exists()
            raise OSError('simulated crash')
        return original(path, *a, **kw)
    monkeypatch.setattr(assignment_publisher, 'atomic_json', fail)
    with pytest.raises(OSError): asyncio.run(assignment_publisher.publish(args))
    monkeypatch.setattr(assignment_publisher, 'atomic_json', original)
    args.contest_pool_from_chain_epoch = args.contest_pool_hotkey = args.contest_pool_coldkey = None
    result = asyncio.run(assignment_publisher.publish(args))
    assert json.loads(Path(result['publicFile']).read_text())['payload']['schema'] == assignment.ROTATING_SCHEMA
    args.contest_pool_hotkey = hot(7); args.contest_pool_coldkey = hot(17); args.contest_pool_from_chain_epoch = 21
    with pytest.raises(ValueError, match='activation_conflict'): asyncio.run(assignment_publisher.publish(args))


def test_immediate_activation_rejected(publisher_case, contest):
    args = publisher_case
    args.contest_pool_from_chain_epoch = 20; args.contest_pool_hotkey = hot(6); args.contest_pool_coldkey = hot(16)
    with pytest.raises(ValueError, match='must_be_future'): asyncio.run(assignment_publisher.publish(args))


def test_publisher_activates_pinned_policy_and_preserves_old_assignments(publisher_case, contest):
    args = publisher_case
    args.contest_pool_from_chain_epoch = 21; args.contest_pool_hotkey = hot(6); args.contest_pool_coldkey = hot(16)
    first = asyncio.run(assignment_publisher.publish(args))
    old = Path(first['publicFile']).read_bytes()
    case = contest[0]
    # Same finalized accounting window in the next chain slot: explicit reuse,
    # so the work report cannot be paid for a second time.
    case[3]['SubnetEpochIndex'] = 21
    args.contest_pool_from_chain_epoch = args.contest_pool_hotkey = args.contest_pool_coldkey = None
    new = asyncio.run(assignment_publisher.publish(args))
    p = json.loads(Path(new['publicFile']).read_text())['payload']
    assert p['schema'] == assignment.CONTEST_SCHEMA and p['contestPool']['uid'] == 6
    assert Path(first['publicFile']).read_bytes() == old
    assert asyncio.run(assignment_publisher.publish(args)) == new


def test_pool_pin_symlink_cannot_hide_downgrade(contest, tmp_path):
    case, selected, _, Client = contest
    (case[0]/'contest-pool-policy.json').symlink_to(tmp_path/'absent')
    with pytest.raises(OSError): asyncio.run(pool.verify(Client(), selected, case[0]))


def test_operator_acceptance_does_not_change_signing_consent(contest):
    root = contest[0][0]
    auth = (root/'authorization.json').read_bytes()
    pool.accept(root, {'hotkey':hot(6), 'coldkey':hot(16)})
    assert (root/'authorization.json').read_bytes() == auth
    with pytest.raises(ValueError, match='conflict'): pool.accept(root, {'hotkey':hot(7), 'coldkey':hot(17)})


def test_reconciliation_requires_correct_pool_ownership():
    value = {'policy': pool.POLICY, 'snapshotBlock': 10, 'snapshotBlockHash': '0x'+'a'*64,
             'ownerColdkey': hot(10), 'uid': 6, 'hotkey': hot(6), 'coldkey': hot(16)}
    record = {'policy': pool.WEIGHT_POLICY, 'commitReveal': False,
              'plan': {'quantized': {'6': 65535}, 'identities': {'6': hot(6)}, 'contestPool': value}}
    args = (record, {'extrinsic_id': '10-0'}, [(6, 65535)], 10, {6: hot(6)})
    assert not observation_matches(*args)
    assert observation_matches(*args, contest_ownership={'owner': hot(10), 'hotkey': hot(6), 'coldkey': hot(16), 'uid': 6})
