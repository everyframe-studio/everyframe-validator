"""Deterministic owner burns: real signatures, fake chain, no paid operations."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption

from everyframe_validator import assignment, assignment_publisher, application, cadence, runner
from everyframe_validator.assignment_publisher import publish
from everyframe_validator.core import burn_selection as burn
from everyframe_validator.core.chain_scope import scope
from everyframe_validator.core.reward_policy import digest
from everyframe_validator.core.validator import epoch_weight_plan
from everyframe_validator.publish import sign
from everyframe_validator.state import initialize, journal_rows
from conftest import hot
from test_scheduled import scheduled_case, report_for, run


def owner_neurons():
    return [{'uid': i, 'hotkey': hot(i), 'coldkey': hot(10) if i in (0, 4, 5) else hot(i+10),
             'validator_permit': i in (0, 1)} for i in range(6)]


def make_assignment(case, payload, signing, *, reuse=False):
    root, config, view, s = case
    if reuse:
        s.update(LastEpochBlock=3970, previousChainEpochStart=3610, chainEpochStartHash='0x'+'b'*64)
    epoch, mode = assignment.target(view, s)
    raw = report_for(payload, epoch, signing)
    from everyframe_validator.feed import verify
    report = verify(raw, signing[1], scope('test'), epoch) if mode == 'signed_epoch' else None
    base = assignment.project(scope('test'), view, s, report)
    selection = burn.derive(base, hot(10), owner_neurons())
    selected = assignment.project(scope('test'), view, s, report, burn_selection=selection)
    return raw, json.dumps(sign(selected, signing[0])).encode(), selected


@pytest.fixture
def rotation(scheduled_case, payload, signing, monkeypatch):
    import bittensor as bt
    case = scheduled_case
    case[2]['neurons'] = owner_neurons()
    original = bt.Client
    calls = []

    class Client(original):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            prior = self.query

            async def query(storage, args, block=None):
                calls.append((storage[1], block))
                if storage[1] == 'OwnedHotkeys':
                    return [hot(0), hot(4), hot(5)]
                if storage[1] == 'Owner':
                    return hot(10) if args[0] in [hot(0), hot(4), hot(5)] else hot(20)
                return await prior(storage, args, block=block)

            async def metagraph(netuid, block=None, commitments=False):
                calls.append(('metagraph', block))
                return SimpleNamespace(block=block, neurons=[SimpleNamespace(**n) for n in owner_neurons()])
            self.query = query
            self.subnets.metagraph = metagraph

    monkeypatch.setattr(bt, 'Client', Client)
    raw, envelope, selected = make_assignment(case, payload, signing)
    monkeypatch.setattr(assignment, 'fetch', lambda url, epoch: envelope if '/allocations/' in url else raw)
    return case, raw, envelope, selected, calls, Client


def test_all_operators_derive_same_target_despite_order_and_validator_identity(rotation):
    case, _, _, selected, _, _ = rotation
    base = {k: v for k, v in selected.items() if k != 'burnSelection'}
    chosen = selected['burnSelection']
    assert burn.derive(base, hot(10), list(reversed(owner_neurons()))) == chosen
    assert chosen['uid'] in (0, 4, 5)
    assert chosen['candidateCount'] == 3
    first = run(case, submit=False)
    second = case[0].parent/'second-owner'
    initialize(second, {**case[1], 'validatorHotkey': hot(0)})
    other = asyncio.run(runner.tick(second, submit=False, weight_cadence='chain', weight_interval_blocks=100))
    assert first['weights'] == other['weights']
    assert first['burnUid'] == other['burnUid'] == chosen['uid']
    assert first['assignmentPolicy'] == assignment.ROTATING_POLICY


def test_only_unused_allocation_moves_and_miner_weights_are_identical(rotation, payload):
    _, _, _, assigned, _, _ = rotation
    args = (payload['artifact'], payload['bindings'], owner_neurons(), hot(1),
            {'min_allowed_weights': 1, 'max_weights_limit': 65535}, hot(10), hot(0))
    original = epoch_weight_plan(*args)
    selected = epoch_weight_plan(*args, burn_selection=assigned['burnSelection'])
    for uid in (2, 3):
        assert original['weights'][uid] == selected['weights'][uid]
        assert original['quantized'][uid] == selected['quantized'][uid]
    assert original['burnBudgetUnits'] == selected['burnBudgetUnits']
    assert sum(selected['weights'].values()) == 65535
    assert selected['identities'][selected['burnUid']] == selected['burnHotkey']


def test_no_unused_budget_has_no_effect_on_serving_weights(rotation, payload):
    _, _, _, assigned, _, _ = rotation
    a = deepcopy(payload['artifact'])
    for row in a['window']['scores']:
        row['earningsMicrousd'] *= 10
    args = (a, payload['bindings'], owner_neurons(), hot(1),
            {'min_allowed_weights': 1, 'max_weights_limit': 65535}, hot(10), hot(0))
    a, b = epoch_weight_plan(*args), epoch_weight_plan(*args, burn_selection=assigned['burnSelection'])
    assert a['burnBudgetUnits'] == b['burnBudgetUnits'] == 0
    assert a['weights'] == b['weights']


def test_snapshot_is_stable_but_report_epoch_and_candidates_are_domain_separated(rotation):
    assigned = rotation[3]
    seen = set()
    for epoch in range(20, 120):
        p = {**assigned, 'chainEpoch': epoch}
        choice = burn.derive(p, hot(10), owner_neurons())
        seen.add(choice['uid'])
        assert choice == burn.derive(p, hot(10), owner_neurons())
    assert seen == {0, 4, 5}
    original = assigned['burnSelection']
    for field, value in [('payloadHash', 'b'*64), ('genesis', '0x'+'d'*64), ('chainEpochStartHash', '0x'+'e'*64)]:
        assert burn.derive({**assigned, field: value}, hot(10), owner_neurons())['seedHash'] != original['seedHash']
    assert burn.derive(assigned, hot(10), owner_neurons()[:-1])['candidateHash'] != original['candidateHash']


@pytest.mark.parametrize('change', ['duplicate_uid', 'duplicate_hotkey', 'missing_owner', 'bool_uid'])
def test_invalid_candidate_graph_rejected(rotation, change):
    ns = owner_neurons()
    if change == 'duplicate_uid': ns[-1]['uid'] = 0
    if change == 'duplicate_hotkey': ns[-1]['hotkey'] = hot(0)
    if change == 'missing_owner': ns = [n for n in ns if n['coldkey'] != hot(10)]
    if change == 'bool_uid': ns[-1]['uid'] = True
    with pytest.raises(ValueError): burn.derive(rotation[3], hot(10), ns)


def test_one_owner_is_valid_without_claiming_rotation(rotation):
    ns = [n for n in owner_neurons() if n['uid'] < 4]
    choice = burn.derive(rotation[3], hot(10), ns)
    assert choice['candidateCount'] == 1 and choice['uid'] == 0


def test_rejection_sampling_is_deterministic_and_handles_non_power_of_two():
    assert burn.selection_index('f'*64, 3) in (0, 1, 2)
    assert burn.selection_index('f'*64, 3) == burn.selection_index('f'*64, 3)
    assert burn.selection_index('f'*64, 1) == 0


def test_later_candidate_set_changes_do_not_reroll_a_verified_choice(rotation):
    choice = rotation[3]['burnSelection']
    ns = [n for n in owner_neurons() if n['coldkey'] != hot(10) or n['uid'] == choice['uid']]
    assert burn.resolve(ns, hot(10), hot(0), choice)['uid'] == choice['uid']
    ns.append({'uid': 9, 'hotkey': hot(9), 'coldkey': hot(10)})
    assert burn.resolve(ns, hot(10), hot(0), choice)['uid'] == choice['uid']


@pytest.mark.parametrize('change', ['owner', 'uid', 'key'])
def test_selected_recipient_identity_must_remain_bound(rotation, change):
    choice = rotation[3]['burnSelection']
    ns = owner_neurons()
    n = next(n for n in ns if n['uid'] == choice['uid'])
    if change == 'owner': n['coldkey'] = hot(20)
    if change == 'uid': n['uid'] = 20
    if change == 'key': n['hotkey'] = hot(20)
    with pytest.raises(ValueError, match='ownership_changed'):
        burn.resolve(ns, hot(10), hot(0), choice)


def test_selection_uses_pinned_chain_state_and_cache_is_verified(rotation):
    case, _, _, selected, calls, Client = rotation
    client = Client()
    root = case[0]
    asyncio.run(assignment.verify_burn(client, selected, root))
    assert not (root/'verified-burns').exists() and not (root/'burn-policy.json').exists()
    assert {b for _, b in calls} == {selected['chainEpochStart']}
    asyncio.run(assignment.verify_burn(client, selected, root, persist=True))
    before = list(calls)
    asyncio.run(assignment.verify_burn(client, selected, root, persist=True))
    assert calls == before
    changed = deepcopy(selected); changed['burnSelection']['uid'] = 2
    with pytest.raises(ValueError, match='conflict'):
        asyncio.run(assignment.verify_burn(client, changed, root))


def test_even_signed_wrong_choice_rejected_by_independent_chain_check(rotation, signing):
    case, _, _, selected, _, Client = rotation
    forged = deepcopy(selected); forged['burnSelection'].update(uid=2, hotkey=hot(2))
    raw = json.dumps(sign(forged, signing[0])).encode()
    checked = assignment.authenticate(raw, signing[1], scope('test'), case[2], case[3])
    with pytest.raises(ValueError, match='does_not_match_chain'):
        asyncio.run(assignment.verify_burn(Client(), checked))
    envelope = json.loads(raw); envelope['payload']['burnSelection']['uid'] = 3
    with pytest.raises(InvalidSignature):
        assignment.authenticate(json.dumps(envelope), signing[1], scope('test'), case[2], case[3])


@pytest.mark.parametrize('field,value', [('candidateCount', True), ('uid', True), ('snapshotBlock', 4000),
                                       ('seedHash', '0'*64), ('policy', 'random-local')])
def test_invalid_signed_selection_metadata_rejected(rotation, signing, field, value):
    case, _, _, selected, _, _ = rotation
    forged = deepcopy(selected); forged['burnSelection'][field] = value
    with pytest.raises(ValueError):
        assignment.authenticate(json.dumps(sign(forged, signing[0])), signing[1], scope('test'), case[2], case[3])


def test_owned_hotkeys_inconsistent_with_metagraph_blocks(rotation):
    client = rotation[-1]()
    prior = client.query
    async def query(storage, args, block=None):
        return [hot(0)] if storage[1] == 'OwnedHotkeys' else await prior(storage, args, block=block)
    client.query = query
    with pytest.raises(ValueError, match='owner_mapping_inconsistent'):
        asyncio.run(assignment.verify_burn(client, rotation[3]))


def test_no_silent_downgrade_after_rotation_is_seen(rotation):
    case, _, _, selected, _, Client = rotation
    asyncio.run(assignment.verify_burn(Client(), selected, case[0], persist=True))
    legacy = {k: v for k, v in selected.items() if k != 'burnSelection'}
    legacy.update(schema=assignment.SCHEMA, policy=assignment.POLICY)
    with pytest.raises(ValueError, match='downgrade'):
        asyncio.run(assignment.verify_burn(Client(), legacy, case[0]))
    legacy['chainEpoch'] -= 1
    asyncio.run(assignment.verify_burn(Client(), legacy, case[0]))


def test_changed_current_owner_does_not_reroll_before_broadcast(rotation, chain, monkeypatch):
    case = rotation[0]
    prior = cadence.snapshot
    async def snapshot(*args):
        v = deepcopy(await prior(*args)); v['owner'] = hot(20); return v
    monkeypatch.setattr(cadence, 'snapshot', snapshot)
    with pytest.raises(ValueError, match='ownership_changed'):
        run(case)
    chain.execute.assert_not_called()
    assert journal_rows(case[0]) == []


def test_restart_preserves_choice_and_does_not_submit_twice(rotation, chain):
    case = rotation[0]
    first = run(case)
    assert first['state'] == 'commit_finalized'
    row = journal_rows(case[0])[0]
    assert row['record']['plan']['burnSelection'] == rotation[3]['burnSelection']
    assert run(case)['reason'] == 'chain_epoch_already_committed'
    assert chain.execute.await_count == 1


def test_explicit_reuse_burn_uses_same_rotating_selection(rotation, payload, signing, monkeypatch):
    case = rotation[0]
    _, envelope, selected = make_assignment(case, payload, signing, reuse=True)
    monkeypatch.setattr(assignment, 'fetch', lambda url, epoch: envelope if '/allocations/' in url else pytest.fail('no report for reuse'))
    result = run(case, submit=False)
    assert result['allBurn']
    assert result['weights'] == {selected['burnSelection']['uid']: 65535}
    assert result['burnUid'] == selected['burnSelection']['uid']


def test_publisher_activation_is_explicit_and_durable(rotation, signing, payload, monkeypatch, tmp_path, chain):
    case, raw, _, _, _, _ = rotation
    monkeypatch.setattr('everyframe_validator.assignment_publisher.schedule', lambda *a: asyncio.sleep(0, result=case[3]))
    epochs = tmp_path/'reports'; epochs.mkdir(); (epochs/'10.json').write_bytes(raw)
    key = tmp_path/'signing.pem'; key.write_bytes(signing[0].private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())); key.chmod(0o600)
    args = SimpleNamespace(network='testnet', expected_public_key=signing[1], signing_key=str(key),
                           epochs_dir=str(epochs), output=str(tmp_path/'published'), burn_rotation_from_chain_epoch=20)
    first = asyncio.run(publish(args))
    p = json.loads(Path(first['publicFile']).read_text())['payload']
    assert p['schema'] == assignment.ROTATING_SCHEMA
    args.burn_rotation_from_chain_epoch = None
    assert asyncio.run(publish(args)) == first
    args.burn_rotation_from_chain_epoch = 21
    with pytest.raises(ValueError, match='activation_conflict'):
        asyncio.run(publish(args))
    chain.execute.assert_not_called()


@pytest.mark.parametrize('activation', [None, 21])
def test_publisher_stays_legacy_without_reached_activation(rotation, signing, monkeypatch, tmp_path, activation):
    case, raw, _, _, _, _ = rotation
    monkeypatch.setattr('everyframe_validator.assignment_publisher.schedule', lambda *a: asyncio.sleep(0, result=case[3]))
    epochs = tmp_path/'reports'; epochs.mkdir(); (epochs/'10.json').write_bytes(raw)
    key = tmp_path/'key.pem'; key.write_bytes(signing[0].private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())); key.chmod(0o600)
    args = SimpleNamespace(network='testnet', expected_public_key=signing[1], signing_key=str(key),
                           epochs_dir=str(epochs), output=str(tmp_path/'published'), burn_rotation_from_chain_epoch=activation)
    result = asyncio.run(publish(args))
    p = json.loads(Path(result['publicFile']).read_text())['payload']
    assert p['schema'] == assignment.SCHEMA and 'burnSelection' not in p


@pytest.fixture
def publisher_case(rotation, signing, monkeypatch, tmp_path):
    case, raw, _, _, _, _ = rotation
    monkeypatch.setattr(assignment_publisher, 'schedule', lambda *a: asyncio.sleep(0, result=case[3]))
    epochs = tmp_path/'reports'; epochs.mkdir(); (epochs/'10.json').write_bytes(raw)
    key = tmp_path/'key.pem'
    key.write_bytes(signing[0].private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    key.chmod(0o600)
    return SimpleNamespace(network='testnet', expected_public_key=signing[1], signing_key=str(key),
                           epochs_dir=str(epochs), output=str(tmp_path/'published'), burn_rotation_from_chain_epoch=20)


def test_publisher_crash_after_policy_pin_resumes_without_flag(publisher_case, monkeypatch):
    real_write = assignment_publisher.atomic_json
    output = Path(publisher_case.output)
    def fail_assignment(path, *args, **kwargs):
        if path.name == '20.json':
            assert (output/'burn-activation.json').is_file()
            raise OSError('simulated interrupted assignment write')
        return real_write(path, *args, **kwargs)
    monkeypatch.setattr(assignment_publisher, 'atomic_json', fail_assignment)
    with pytest.raises(OSError, match='interrupted'):
        asyncio.run(publish(publisher_case))
    assert not (output/'20.json').exists()
    monkeypatch.setattr(assignment_publisher, 'atomic_json', real_write)
    publisher_case.burn_rotation_from_chain_epoch = None
    result = asyncio.run(publish(publisher_case))
    assert json.loads(Path(result['publicFile']).read_text())['payload']['schema'] == assignment.ROTATING_SCHEMA


def test_publisher_failed_policy_pin_never_exposes_v2(publisher_case, monkeypatch):
    real_write = assignment_publisher.atomic_json
    def fail_pin(path, *args, **kwargs):
        if path.name == 'burn-activation.json':
            raise OSError('simulated interrupted policy write')
        return real_write(path, *args, **kwargs)
    monkeypatch.setattr(assignment_publisher, 'atomic_json', fail_pin)
    with pytest.raises(OSError, match='interrupted'):
        asyncio.run(publish(publisher_case))
    assert not (Path(publisher_case.output)/'20.json').exists()


def test_publisher_conflicting_current_assignment_does_not_pin_policy(publisher_case):
    publisher_case.burn_rotation_from_chain_epoch = None
    first = asyncio.run(publish(publisher_case))
    previous = Path(first['publicFile']).read_bytes()
    publisher_case.burn_rotation_from_chain_epoch = 20
    with pytest.raises(ValueError, match='immutable_file_conflict'):
        asyncio.run(publish(publisher_case))
    assert Path(first['publicFile']).read_bytes() == previous
    assert not (Path(publisher_case.output)/'burn-activation.json').exists()


def test_concurrent_publishers_cannot_race_activation(publisher_case, rotation, monkeypatch):
    async def scenario():
        entered, resume = asyncio.Event(), asyncio.Event()
        async def delayed_schedule(*args):
            entered.set()
            await resume.wait()
            return rotation[0][3]
        monkeypatch.setattr(assignment_publisher, 'schedule', delayed_schedule)
        first = asyncio.create_task(publish(publisher_case))
        await asyncio.wait_for(entered.wait(), 5)
        try:
            with pytest.raises(ValueError, match='publisher_busy'):
                await publish(SimpleNamespace(**{**vars(publisher_case), 'burn_rotation_from_chain_epoch': None}))
        finally:
            resume.set()
        await first
    asyncio.run(scenario())
    # A completed process releases the lock; retries preserve the same decision.
    publisher_case.burn_rotation_from_chain_epoch = None
    asyncio.run(publish(publisher_case))
    p = json.loads((Path(publisher_case.output)/'20.json').read_text())['payload']
    assert p['schema'] == assignment.ROTATING_SCHEMA


@pytest.mark.parametrize('malformed', [None, [], 'invalid', 17, {'policy': 'wrong'}])
def test_malformed_policy_pins_fail_clearly(publisher_case, rotation, malformed):
    from everyframe_validator.state import atomic_json
    output = Path(publisher_case.output); output.mkdir()
    atomic_json(output/'burn-activation.json', malformed)
    with pytest.raises(ValueError, match='invalid_burn_policy_pin'):
        asyncio.run(publish(publisher_case))
    root = rotation[0][0]
    atomic_json(root/'burn-policy.json', malformed)
    with pytest.raises(ValueError, match='invalid_burn_policy_pin'):
        asyncio.run(assignment.verify_burn(rotation[-1](), rotation[3], root))


@pytest.mark.parametrize('change_at', [None, 100, 150])
def test_application_verifies_burn_owner_at_reveal_and_mechanism_step(rotation, payload, change_at):
    selected = rotation[3]['burnSelection']
    plan = epoch_weight_plan(payload['artifact'], payload['bindings'], owner_neurons(), hot(1),
                             {'min_allowed_weights': 1, 'max_weights_limit': 65535}, hot(10), hot(0), burn_selection=selected)
    row = {'record': {'netuid': 566, 'validatorHotkey': hot(1), 'plan': plan, 'commitReveal': False},
           'result': {'extrinsic_id': '100-0'}}
    async def query(storage, args, block=None):
        name = storage[1]
        if name == 'Weights': return list(plan['quantized'].items())
        if name == 'Keys': return hot(args[1])
        if name == 'SubnetOwner': return hot(10)
        if name == 'Owner': return hot(20) if change_at is not None and block >= change_at else hot(10)
        if name == 'LastMechansimStepBlock': return 150 if block >= 150 else 90
        if name == 'Emission': return [0]*6
        raise AssertionError(name)
    client = SimpleNamespace(query=query, _substrate=SimpleNamespace(block_hash=AsyncMock(return_value='0x'+'a'*64)))
    result = asyncio.run(application.inspect(client, row, 200, []))
    assert result.get('applicationConfirmed', False) is (change_at is None)
    assert result['alphaSettlementConfirmed'] is False
    if change_at is not None:
        assert result['state'] in ('reveal_vector_mismatch', 'not_applied_at_next_step')
