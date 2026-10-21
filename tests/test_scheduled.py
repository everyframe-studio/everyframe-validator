import asyncio
import json
import sqlite3
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from everyframe_validator import cadence, scheduled, application, runner
from everyframe_validator.feed import FeedUnavailable
from everyframe_validator.state import journal_rows
from everyframe_validator.publish import sign
from test_runner import authorize
from conftest import hot


@pytest.fixture
def scheduled_case(profile, chain, monkeypatch):
    root, config = profile
    authorize(root, config, maximum=20)
    view = dict(block=4000, blockHash='0x'+'a'*64, params=chain.params,
                neurons=[vars(n) for n in chain.neurons], owner=hot(10), ownerHotkey=hot(0),
                lastUpdate=0, pending=[], epoch=10)
    s = dict(SubnetEpochIndex=20, LastEpochBlock=3960, LastMechansimStepBlock=3960,
             PendingEpochAt=0, RevealPeriodEpochs=2, Timestamp=1_700_000_000_000,
             epochStartedAtMs=1_700_000_000_000 - 40 * 12000)
    monkeypatch.setattr(cadence, 'snapshot', AsyncMock(side_effect=lambda *a: dict(view)))
    monkeypatch.setattr(scheduled, 'schedule', AsyncMock(side_effect=lambda *a: dict(s)))
    monkeypatch.setattr(scheduled, 'fetch', lambda *a: (_ for _ in ()).throw(FeedUnavailable()))
    monkeypatch.setattr(application, 'observe', AsyncMock(return_value=[]))
    chain.preview.return_value.extras = {'reveal_round': 40_000_000}
    return root, config, view, s


def run(case, submit=True, missing='burn'):
    return asyncio.run(runner.tick(case[0], submit=submit, weight_cadence='chain',
                                  weight_interval_blocks=100, missing_accounting=missing))


def advance(case, chain):
    case[2].update(block=4360, epoch=11)
    case[3].update(SubnetEpochIndex=21, LastEpochBlock=4320, LastMechansimStepBlock=4320)
    chain.preview.return_value.extras = {'reveal_round': 40_001_440}
    data = dict(success=True, block_hash='0x'+'c'*64, extrinsic_id='4360-0')
    chain.execute.return_value = SimpleNamespace(**data, to_dict=lambda: data)


def test_bad_wallet_fails_before_epoch_wait_without_signing(scheduled_case, chain):
    from pathlib import Path
    from everyframe_validator.wallet import WalletConfigurationError
    scheduled_case[3]['LastEpochBlock'] = scheduled_case[2]['block'] - 1
    Path(chain.wallet.hotkey_file.path).unlink()
    with pytest.raises(WalletConfigurationError, match='wallet_hotkey_missing'):
        run(scheduled_case)
    chain.execute.assert_not_called()
    chain.preview.assert_not_called()
    assert journal_rows(scheduled_case[0]) == []
    status = json.loads((scheduled_case[0] / 'status.json').read_text())
    assert status['code'] == 'wallet_hotkey_missing'
    assert status['walletPaths']['hotkeyFile'] == chain.wallet.hotkey_file.path
    cursor = json.loads((scheduled_case[0] / 'reward-schedule.json').read_text())
    assert cursor['startEpoch'] == scheduled_case[2]['epoch']


def test_actual_signer_identity_checked_before_reservation(scheduled_case, chain):
    chain.wallet.get_hotkey = lambda _: SimpleNamespace(ss58_address=hot(3))
    with pytest.raises(ValueError, match='wallet does not match'):
        run(scheduled_case)
    chain.execute.assert_not_called()
    chain.preview.assert_not_called()
    assert journal_rows(scheduled_case[0]) == []


def test_uncertain_recovery_precedes_wallet_preflight(scheduled_case, chain, monkeypatch):
    from pathlib import Path
    chain.execute.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        run(scheduled_case)
    before = journal_rows(scheduled_case[0])
    assert before[0]['state'] == 'unknown'
    Path(chain.wallet.hotkey_file.path).unlink()
    recovery = AsyncMock(return_value={'state': 'unknown'})
    monkeypatch.setattr(scheduled, 'recover', recovery)
    result = run(scheduled_case)
    assert result['state'] == 'blocked' and result['reason'] == 'uncertain_submission'
    recovery.assert_awaited_once()
    assert journal_rows(scheduled_case[0]) == before
    assert chain.execute.await_count == 1


def test_once_per_chain_epoch_even_with_changed_accounting(scheduled_case, chain):
    assert run(scheduled_case)['state'] == 'commit_finalized'
    scheduled_case[2].update(block=4100, epoch=11)
    assert run(scheduled_case)['reason'] == 'chain_epoch_already_committed'
    assert chain.execute.await_count == 1


def test_late_report_waits_next_chain_epoch_and_retains_cursor(scheduled_case, chain, payload, signing, monkeypatch):
    assert run(scheduled_case)['accountingSource'] == 'missing_epoch_burn'
    monkeypatch.setattr(scheduled, 'fetch', lambda *a: json.dumps(sign(payload, signing[0])).encode())
    scheduled_case[2].update(block=4100)
    assert run(scheduled_case)['reason'] == 'chain_epoch_already_committed'
    advance(scheduled_case, chain)
    result = run(scheduled_case)
    assert result['accountingSource'] == 'signed_epoch'
    assert result['epoch'] == 10  # not discarded for the newer empty epoch 11
    assert chain.execute.await_count == 2


def test_signed_report_never_repeated(scheduled_case, chain, payload, signing, monkeypatch):
    monkeypatch.setattr(scheduled, 'fetch', lambda *a: json.dumps(sign(payload, signing[0])).encode())
    assert run(scheduled_case)['epoch'] == 10
    advance(scheduled_case, chain)
    monkeypatch.setattr(scheduled, 'fetch', lambda *a: (_ for _ in ()).throw(FeedUnavailable()))
    assert run(scheduled_case)['nextAccountingEpoch'] == 11
    assert journal_rows(scheduled_case[0])[0]['result']['alphaSettlementConfirmed'] is False


def report_for(payload, epoch, signing, empty=False):
    p = deepcopy(payload)
    a = p['artifact']
    a.update(epoch=epoch, startBlock=epoch*361, endBlock=(epoch+1)*361,
             finalizedBlock=(epoch+1)*361+4,
             closeBlockHash='0x'+('b' if epoch == 10 else 'a')*64)
    if empty:
        a['window']['scores'] = []
        p['bindings'] = {}
    return json.dumps(sign(p, signing[0])).encode()


def test_gap_does_not_block_newer_report_and_late_gap_is_not_lost(scheduled_case, chain, payload, signing, monkeypatch):
    # Epoch 10 was absent; its burn must not consume the late report.
    run(scheduled_case)
    advance(scheduled_case, chain)
    seen = []
    def fetch_new(feed, epoch):
        seen.append(epoch)
        if epoch == 10:
            raise FeedUnavailable()
        return report_for(payload, epoch, signing)
    monkeypatch.setattr(scheduled, 'fetch', fetch_new)
    result = run(scheduled_case)
    assert result['epoch'] == 11 and not result['allBurn']
    assert result['missingAccountingEpochs'] == [10]
    assert seen == [10, 11]
    scheduled_case[2].update(block=4720, epoch=12)
    scheduled_case[3].update(SubnetEpochIndex=22, LastEpochBlock=4680)
    chain.preview.return_value.extras = {'reveal_round': 40_002_880}
    data = dict(success=True, block_hash='0x'+'c'*64, extrinsic_id='4720-0')
    chain.execute.return_value = SimpleNamespace(**data, to_dict=lambda: data)
    monkeypatch.setattr(scheduled, 'fetch', lambda _, e: report_for(payload, e, signing))
    result = run(scheduled_case)
    assert result['epoch'] == 10 and not result['allBurn']
    assert [r['record']['epoch'] for r in journal_rows(scheduled_case[0])
            if r['record']['accountingSource'] == 'signed_epoch'] == [11, 10]


def scan_case(case, chain, first=10, persist=True):
    import bittensor as bt
    from everyframe_validator.core.chain_scope import scope
    root, config, view, _ = case
    return asyncio.run(scheduled.available_accounting(bt.Client(), root, config,
        scope(config['network'], config['netuid']), view, journal_rows(root), first, persist))


def test_authenticated_empty_report_does_not_delay_paid_work(scheduled_case, chain, payload, signing, monkeypatch):
    scheduled_case[2].update(block=4720, epoch=12)
    def fetch(_, e):
        if e == 10: raise FeedUnavailable()
        return report_for(payload, e, signing, empty=e == 11)
    monkeypatch.setattr(scheduled, 'fetch', fetch)
    p, epoch, info = scan_case(scheduled_case, chain)
    assert epoch == 12 and p['artifact']['window']['scores']
    assert info['missingAccountingEpochs'] == [10]
    assert info['emptyAccountingEpochs'] == [11]
    chain.execute.assert_not_called()


@pytest.mark.parametrize('failure', [ValueError('invalid signature'), TimeoutError('transport'), RuntimeError('HTTP 500')])
def test_gap_scan_never_treats_errors_as_absence(scheduled_case, chain, failure, monkeypatch):
    scheduled_case[2].update(block=4720, epoch=12)
    seen = []
    def fetch(_, e):
        seen.append(e)
        if e == 10: raise FeedUnavailable()
        raise failure
    monkeypatch.setattr(scheduled, 'fetch', fetch)
    with pytest.raises(type(failure)):
        scan_case(scheduled_case, chain)
    assert seen == [10, 11]
    chain.execute.assert_not_called()


def test_gap_scan_invalid_signed_report_stops_before_newer_work(scheduled_case, chain, payload, signing, monkeypatch):
    scheduled_case[2].update(block=4720, epoch=12)
    def fetch(_, e):
        if e == 10: raise FeedUnavailable()
        p = json.loads(report_for(payload, e, signing))
        p['payload']['artifact']['minerShare'] = '1'
        return json.dumps(p).encode()
    monkeypatch.setattr(scheduled, 'fetch', fetch)
    from cryptography.exceptions import InvalidSignature
    with pytest.raises(InvalidSignature):
        scan_case(scheduled_case, chain)
    assert not (scheduled_case[0] / 'accounting-scan.json').exists()


def test_many_missing_reports_rotate_without_burning_or_forgetting(scheduled_case, chain, payload, signing, monkeypatch):
    scheduled_case[2].update(block=20000, epoch=50)
    def fetch(_, e):
        if e != 50: raise FeedUnavailable()
        return report_for(payload, e, signing)
    monkeypatch.setattr(scheduled, 'fetch', fetch)
    p, _, info = scan_case(scheduled_case, chain)
    assert p is None and not info['accountingScanComplete']
    assert len(info['missingAccountingEpochs']) == 32
    p, epoch, _ = scan_case(scheduled_case, chain)
    assert epoch == 50 and p is not None
    assert json.loads((scheduled_case[0]/'accounting-scan.json').read_text())['nextEpoch'] == 10
    assert not journal_rows(scheduled_case[0])


def test_dry_gap_scan_does_not_change_scan_position(scheduled_case, chain):
    scheduled_case[2].update(block=20000, epoch=50)
    scan_case(scheduled_case, chain, persist=False)
    assert not (scheduled_case[0] / 'accounting-scan.json').exists()


def test_actual_scan_deadline_waits_without_signing(scheduled_case, chain, monkeypatch):
    async def slow(*args):
        await asyncio.sleep(1)
    monkeypatch.setattr(scheduled, 'available_accounting', slow)
    monkeypatch.setattr(scheduled, 'ACCOUNTING_SCAN_TIMEOUT_SECONDS', 0.001)
    result = run(scheduled_case)
    assert result['reason'] == 'accounting_scan_timeout'
    chain.execute.assert_not_called()
    assert not journal_rows(scheduled_case[0])


@pytest.mark.parametrize('offset', [0, 4, 91, 355, 360])
def test_early_window_bounds(scheduled_case, chain, offset):
    scheduled_case[2]['block'] = 3960 + offset
    assert run(scheduled_case)['reason'] == 'waiting_for_chain_epoch_window'
    chain.execute.assert_not_called()


def test_cursor_stored_while_outside_window(scheduled_case, chain):
    scheduled_case[2]['block'] = 4100
    assert run(scheduled_case)['state'] == 'waiting'
    advance(scheduled_case, chain)
    assert run(scheduled_case)['epoch'] == 10


def test_dry_run_never_signs_or_adopts_cursor(scheduled_case, chain):
    assert run(scheduled_case, submit=False)['state'] == 'dry_run'
    assert not (scheduled_case[0] / 'reward-schedule.json').exists()
    chain.execute.assert_not_called()
    assert not journal_rows(scheduled_case[0])


def test_legacy_pending_occupies_current_epoch(scheduled_case, chain):
    scheduled_case[2]['pending'] = [{'commit_block': 3970}]
    assert run(scheduled_case)['reason'] == 'chain_epoch_already_committed'
    chain.execute.assert_not_called()


def test_legacy_submitted_accounting_is_not_replayed(scheduled_case, chain):
    root, _, view, _ = scheduled_case
    prior = {'block': 3900, 'epoch': 10, 'accountingSource': 'signed_epoch'}
    with sqlite3.connect(root / 'journal.db') as db:
        db.execute('INSERT INTO weight_runs VALUES(?,?,?,?)',
                   ('old', 'commit_finalized', json.dumps(prior), json.dumps({'extrinsic_id': '3900-0'})))
    result = run(scheduled_case)
    assert result['epoch'] == 11
    assert result['accountingSource'] == 'standby_burn'


def test_unknown_never_retries(scheduled_case, chain):
    chain.execute.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        run(scheduled_case)
    assert run(scheduled_case)['reason'] == 'uncertain_submission'
    assert chain.execute.await_count == 1


def test_atomic_same_slot_and_same_accounting_rejected(scheduled_case, chain):
    run(scheduled_case)
    row = journal_rows(scheduled_case[0])[0]
    record = {**row['record'], 'block': 4100}
    with sqlite3.connect(scheduled_case[0] / 'journal.db') as db:
        with pytest.raises(ValueError, match='chain epoch already'):
            scheduled.reserve(db, 'duplicate', record, {'id': record['authorizationId'], 'maxSubmissions': 20})


def test_atomic_report_cannot_be_consumed_in_two_slots(scheduled_case, chain, payload, signing, monkeypatch):
    monkeypatch.setattr(scheduled, 'fetch', lambda *a: json.dumps(sign(payload, signing[0])).encode())
    run(scheduled_case)
    record = dict(journal_rows(scheduled_case[0])[0]['record'])
    record.update(block=4360, chainEpoch=21, chainEpochStart=4320)
    with sqlite3.connect(scheduled_case[0] / 'journal.db') as db:
        with pytest.raises(ValueError, match='accounting epoch already'):
            scheduled.reserve(db, 'duplicate-report', record, {'id': record['authorizationId'], 'maxSubmissions': 20})


def test_pending_owner_epoch_trigger_defers(scheduled_case, chain):
    scheduled_case[3]['PendingEpochAt'] = 4005
    assert run(scheduled_case)['reason'] == 'pending_epoch_transition'
    chain.execute.assert_not_called()


def test_fee_policy_failure_never_broadcasts(scheduled_case, chain):
    chain.preview.return_value.ok = False
    with pytest.raises(ValueError, match='submission_policy_rejected'):
        run(scheduled_case)
    assert journal_rows(scheduled_case[0])[0]['state'] == 'not_submitted'
    chain.execute.assert_not_called()


def test_authorization_cap_preserved(scheduled_case, chain):
    authorize(scheduled_case[0], scheduled_case[1], maximum=1)
    run(scheduled_case)
    advance(scheduled_case, chain)
    with pytest.raises(ValueError, match='budget exhausted'):
        run(scheduled_case)
    assert chain.execute.await_count == 1


@pytest.mark.parametrize('flag', ['block', 'epoch', 'chain'])
def test_cli_old_flags_cannot_restore_same_epoch_refresh(monkeypatch, flag):
    from everyframe_validator import cli
    called = AsyncMock(return_value=0)
    monkeypatch.setattr(cli, 'run', called)
    monkeypatch.setattr('sys.argv', ['everyframe-validator', 'run', '--state-dir', '/unused',
                                  '--weight-cadence', flag, '--once'])
    with pytest.raises(SystemExit) as exit_:
        cli.main()
    assert exit_.value.code == 0
    assert called.call_args.kwargs['weight_cadence'] == 'chain'


def test_reconcile_dispatch_is_read_only_for_scheduled_allocations(scheduled_case, chain, monkeypatch):
    run(scheduled_case)
    inspect_ = AsyncMock(return_value={'state': 'reconciled', 'submittedTransaction': False})
    monkeypatch.setattr(scheduled, 'reconcile_only', inspect_)
    result = asyncio.run(runner.tick(scheduled_case[0], only_reconcile=True))
    assert result['submittedTransaction'] is False
    assert inspect_.await_count == 1
    assert chain.execute.await_count == 1


@pytest.mark.parametrize('extra', [{}, {'reveal_round': 1}])
def test_unverified_round_never_signed(scheduled_case, chain, extra):
    chain.preview.return_value.extras = extra
    with pytest.raises(ValueError, match='timelock_'):
        run(scheduled_case)
    chain.execute.assert_not_called()
    assert journal_rows(scheduled_case[0])[0]['state'] == 'not_submitted'


def test_near_round_collision_rejected(scheduled_case, chain):
    scheduled_case[2]['pending'] = [{'commit_block': 3800, 'reveal_round': 39_999_999}]
    with pytest.raises(ValueError, match='collision'):
        run(scheduled_case)
    chain.execute.assert_not_called()


def test_slow_prepare_blocks_before_broadcast(scheduled_case, chain):
    async def slow(*args):
        scheduled_case[2]['block'] = 4055
        return chain.preview.return_value
    chain.preview.side_effect = slow
    with pytest.raises(ValueError, match='chain_changed_before_broadcast'):
        run(scheduled_case)
    chain.execute.assert_not_called()
    assert journal_rows(scheduled_case[0])[0]['state'] == 'not_submitted'


@pytest.mark.parametrize('error', [TimeoutError(), ValueError('bad_signature')])
def test_invalid_or_unavailable_network_never_burns(scheduled_case, chain, monkeypatch, error):
    monkeypatch.setattr(scheduled, 'fetch', lambda *a: (_ for _ in ()).throw(error))
    with pytest.raises(type(error)):
        run(scheduled_case)
    chain.execute.assert_not_called()


def test_next_window_pipelines_without_waiting_two_tempos(scheduled_case, chain):
    run(scheduled_case)
    advance(scheduled_case, chain)
    scheduled_case[2]['pending'] = [{'hotkey': hot(1), 'commit_block': 4000, 'reveal_round': 40_000_000}]
    assert run(scheduled_case)['state'] == 'commit_finalized'
    assert chain.execute.await_count == 2


def test_chain_tempo_too_short_is_blocked(scheduled_case, chain):
    chain.params['tempo'] = 100
    assert run(scheduled_case)['state'] == 'blocked'
    chain.execute.assert_not_called()


def test_snapshot_schedule_requires_real_chain_evidence():
    client = SimpleNamespace(query=AsyncMock(return_value=None))
    with pytest.raises(ValueError, match='schedule_unavailable'):
        asyncio.run(scheduled.schedule(client, {'netuid': 117}, 100))


def test_reveal_observation_regression_and_application():
    # Replay the exact observed sequence: reward at 9314995, burn at 9314999,
    # actual prior mechanism step 9314990. The next step cannot apply reward.
    row = {'record': {'netuid': 117, 'validatorHotkey': hot(1), 'commitReveal': True,
                     'plan': {'validatorUid': 1, 'quantized': {0: 65535, 127: 166},
                              'identities': {0: hot(0), 127: hot(127)}}},
           'result': {'extrinsic_id': '9314226-0'}}
    reveal, step = 9314995, 9315350
    class Client:
        overwrite = True
        _substrate = SimpleNamespace(block_hash=AsyncMock(return_value='0x'+'a'*64))
        async def at(self, block):
            async def read(*a, **k):
                return {25800: [{'hotkey': hot(1), 'commit_block': 9314226}]} if block < reveal else {}
            return SimpleNamespace(read=read)
        async def query(self, storage, args, block):
            if storage[1] == 'Events':
                return [{'event': {'module_id': 'SubtensorModule', 'event_id': 'TimelockedWeightsRevealed',
                                   'attributes': [117, hot(1)]}}] if block == reveal else []
            if storage[1] == 'Weights':
                return [[0, 65535]] if self.overwrite and block >= 9314999 else [[0, 65535], [127, 166]]
            if storage[1] == 'Keys': return hot(args[1])
            if storage[1] == 'LastMechansimStepBlock': return 9314990 if block < step else step
            if storage[1] == 'Emission': return [0] * 127 + [12345]
            raise AssertionError(storage)
    client = Client()
    failed = asyncio.run(application.inspect(client, row, step + 10, []))
    assert failed['revealConfirmed'] is True
    assert failed['state'] == 'not_applied_at_next_step'
    assert failed['applicationConfirmed'] is False
    client.overwrite = False
    ok = asyncio.run(application.inspect(client, row, step + 10, []))
    assert ok['applicationConfirmed'] is True
    assert ok['alphaSettlementConfirmed'] is False
    assert ok['emissionRaoByUid']['127'] == 12345


def test_pending_commit_is_not_observed_application():
    row = {'record': {'netuid': 117, 'validatorHotkey': hot(1), 'commitReveal': True,
                     'plan': {'validatorUid': 1}}, 'result': {'extrinsic_id': '100-0'}}
    result = asyncio.run(application.inspect(None, row, 200, [{'hotkey': hot(1), 'commit_block': 100}]))
    assert result['state'] == 'pending_reveal'
    assert result['alphaSettlementConfirmed'] is False
