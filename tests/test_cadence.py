import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.exceptions import InvalidSignature

from everyframe_validator import cadence, runner
from everyframe_validator.feed import FeedUnavailable
from everyframe_validator.state import journal_rows, atomic_json
from test_runner import authorize, wire
from conftest import hot


@pytest.fixture
def paced(profile, chain, monkeypatch):
    root, config = profile
    authorize(root, config, maximum=20)
    view = dict(block=4000, blockHash='0x'+'a'*64, params=chain.params,
                neurons=[vars(n) for n in chain.neurons], owner=hot(10), ownerHotkey=hot(0),
                lastUpdate=0, pending=[], epoch=10)
    monkeypatch.setattr(cadence, 'snapshot', AsyncMock(side_effect=lambda *args: dict(view)))
    monkeypatch.setattr(cadence, 'fetch', lambda *args: (_ for _ in ()).throw(FeedUnavailable()))
    return root, config, view


def run(paced, submit=True, missing='burn'):
    root, _, _ = paced
    return asyncio.run(runner.tick(root, submit=submit, weight_interval_blocks=100, missing_accounting=missing))


def test_missing_epoch_burn_is_explicit_and_journalled(paced, chain):
    result = run(paced)
    assert result['state'] == 'commit_finalized'
    assert result['weights'] == {0: 65535}
    assert result['accountingSource'] == 'missing_epoch_burn'
    assert result['nextEligibleBlock'] == 4100
    row = journal_rows(paced[0])[0]
    assert row['record']['payloadHash'] is None
    assert row['result']['revealConfirmed'] is False
    assert not list((paced[0]/'accepted').glob('*'))


def test_wait_default_does_not_burn(paced, chain):
    assert run(paced, missing='wait')['reason'] == 'newest_epoch_not_published'
    chain.execute.assert_not_called()


def test_no_signing_in_dry_run(paced, chain):
    assert run(paced, submit=False)['state'] == 'dry_run'
    chain.execute.assert_not_called()
    assert journal_rows(paced[0]) == []


def test_same_epoch_can_commit_after_100_not_99_blocks(paced, chain):
    run(paced)
    paced[2]['block'] = 4099
    assert run(paced)['reason'] == 'block_cadence'
    assert chain.execute.await_count == 1
    paced[2]['block'] = 4100
    data = {'success': True, 'block_hash': '0x'+'c'*64, 'extrinsic_id': '4100-0'}
    chain.execute.return_value = SimpleNamespace(**data, to_dict=lambda: data)
    assert run(paced)['state'] == 'commit_finalized'
    assert chain.execute.await_count == 2
    assert len(journal_rows(paced[0])) == 2


def test_higher_chain_rate_wins(paced, chain):
    paced[2]['lastUpdate'] = 3900
    chain.params['weights_rate_limit'] = 200
    result = run(paced)
    assert result['nextEligibleBlock'] == 4100
    assert result['reason'] == 'block_cadence'
    chain.execute.assert_not_called()


def test_pending_chain_limit_waits(paced, chain):
    paced[2]['pending'] = [{}] * 10
    assert run(paced)['reason'] == 'pending_commit_limit'
    chain.execute.assert_not_called()


@pytest.mark.parametrize('error', [TimeoutError(), InvalidSignature(), ValueError('feed_http_error')])
def test_network_or_invalid_feed_never_becomes_burn(paced, chain, monkeypatch, error):
    def fail(*args): raise error
    monkeypatch.setattr(cadence, 'fetch', fail)
    with pytest.raises(type(error)):
        run(paced)
    chain.execute.assert_not_called()
    assert journal_rows(paced[0]) == []


def test_ambiguous_broadcast_blocks_followup(paced, chain):
    chain.execute.side_effect = TimeoutError()
    with pytest.raises(TimeoutError): run(paced)
    assert journal_rows(paced[0])[0]['state'] == 'unknown'
    # The exact-hash recovery remains unconfirmed; never auto-rebroadcast.
    assert run(paced)['state'] == 'blocked'
    assert chain.execute.await_count == 1


def test_failed_preparation_is_not_broadcast(paced, chain):
    chain.preview.side_effect = ValueError('submission_policy_rejected')
    with pytest.raises(ValueError): run(paced)
    assert journal_rows(paced[0])[0]['state'] == 'not_submitted'
    chain.execute.assert_not_called()


def test_authorization_limit_is_not_renewed(paced, chain):
    root, config, view = paced
    authorize(root, config, maximum=1)
    run(paced)
    view['block'] += 100
    with pytest.raises(ValueError, match='budget exhausted'): run(paced)
    assert chain.execute.await_count == 1


def test_burn_owner_and_constraints(paced):
    _, config, v = paced
    with pytest.raises(ValueError, match='burn target'):
        cadence.burn_plan(v['neurons'], config['validatorHotkey'], v['params'], hot(11), v['ownerHotkey'])
    v['params']['max_weights_limit'] = 40000
    with pytest.raises(ValueError, match='constraints'):
        cadence.burn_plan(v['neurons'], config['validatorHotkey'], v['params'], v['owner'], v['ownerHotkey'])


def test_chain_change_before_signing_defers(paced, chain, monkeypatch):
    view = paced[2]
    monkeypatch.setattr(cadence, 'snapshot', AsyncMock(side_effect=[view, {**view, 'epoch': 11}]))
    assert run(paced)['reason'] == 'chain_changed_before_signing'
    chain.execute.assert_not_called()


def test_signed_accounting_remains_preferred(paced, payload, signing, chain, monkeypatch):
    from everyframe_validator.publish import sign
    monkeypatch.setattr(cadence, 'fetch', lambda *args: json.dumps(sign(payload, signing[0])).encode())
    result = run(paced)
    assert result['accountingSource'] == 'signed_epoch'
    assert result['allBurn'] is False
    assert journal_rows(paced[0])[0]['record']['payloadHash']


def epoch_run(paced, submit=True):
    return asyncio.run(runner.tick(paced[0], submit=submit, weight_interval_blocks=100,
                                  missing_accounting='burn', weight_cadence='epoch'))


def test_epoch_signed_report_is_submitted_only_once(paced, payload, signing, chain, monkeypatch):
    from everyframe_validator.publish import sign
    monkeypatch.setattr(cadence, 'fetch', lambda *args: json.dumps(sign(payload, signing[0])).encode())
    assert epoch_run(paced)['state'] == 'commit_finalized'
    paced[2]['block'] = 4200
    assert epoch_run(paced)['reason'] == 'epoch_cadence'
    assert chain.execute.await_count == 1
    # Read-only verification remains possible after a submission.
    assert epoch_run(paced, submit=False)['state'] == 'dry_run'


def test_epoch_waits_for_publication_then_burns_only_once(paced, chain):
    paced[2]['block'] = 3980
    assert epoch_run(paced)['reason'] == 'accounting_publication_grace'
    chain.execute.assert_not_called()
    paced[2]['block'] = 4000
    assert epoch_run(paced)['accountingSource'] == 'missing_epoch_burn'
    paced[2]['block'] = 4200
    assert epoch_run(paced)['reason'] == 'epoch_cadence'
    assert chain.execute.await_count == 1


def test_late_signed_report_can_replace_burn_once(paced, payload, signing, chain, monkeypatch):
    from everyframe_validator.publish import sign
    assert epoch_run(paced)['state'] == 'commit_finalized'
    paced[2]['block'] = 4100
    monkeypatch.setattr(cadence, 'fetch', lambda *args: json.dumps(sign(payload, signing[0])).encode())
    data = {'success': True, 'block_hash': '0x'+'c'*64, 'extrinsic_id': '4100-0'}
    chain.execute.return_value = SimpleNamespace(**data, to_dict=lambda: data)
    assert epoch_run(paced)['accountingSource'] == 'signed_epoch'
    paced[2]['block'] = 4200
    assert epoch_run(paced)['reason'] == 'epoch_cadence'
    assert chain.execute.await_count == 2


def test_next_epoch_does_not_wait_for_previous_reveal(paced, chain):
    epoch_run(paced)
    paced[2].update(block=4360, epoch=11, pending=[{}])
    data = {'success': True, 'block_hash': '0x'+'c'*64, 'extrinsic_id': '4360-0'}
    chain.execute.return_value = SimpleNamespace(**data, to_dict=lambda: data)
    assert epoch_run(paced)['state'] == 'commit_finalized'
    assert chain.execute.await_count == 2


def test_atomic_reservation_blocks_duplicate_epoch_even_after_cooldown(paced, chain):
    import sqlite3
    epoch_run(paced)
    prior = journal_rows(paced[0])[0]['record']
    record = {**prior, 'block': prior['block'] + 100}
    with sqlite3.connect(paced[0]/'journal.db') as db:
        with pytest.raises(ValueError, match='epoch already submitted'):
            cadence.reserve(db, 'duplicate', record, {'id': prior['authorizationId'], 'maxSubmissions': 20})


def test_cli_defaults_epoch_and_one_minute():
    from everyframe_validator.cli import parser
    args = parser().parse_args(['run', '--state-dir', '/unused'])
    assert args.weight_cadence == 'chain'
    assert args.interval == 60
    assert args.weight_interval_blocks == 100


def test_epoch_mode_honors_existing_block_mode_submission(paced, payload, signing, chain, monkeypatch):
    from everyframe_validator.publish import sign
    monkeypatch.setattr(cadence, 'fetch', lambda *args: json.dumps(sign(payload, signing[0])).encode())
    run(paced)
    paced[2]['block'] = 4200
    assert epoch_run(paced)['reason'] == 'epoch_cadence'
    assert chain.execute.await_count == 1


def test_uncertain_epoch_submission_cannot_be_repeated(paced, chain):
    chain.execute.side_effect = TimeoutError()
    with pytest.raises(TimeoutError): epoch_run(paced)
    paced[2]['block'] = 4200
    assert epoch_run(paced)['reason'] == 'uncertain_submission'
    assert chain.execute.await_count == 1
