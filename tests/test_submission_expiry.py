import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from everyframe_validator.core.submission import recover


def run_recovery(tmp_path, *, head=170, cursor=98, mortality=True, missing_archive=False, found=False):
    record = {'block': 100, 'commitReveal': True}
    broadcast = {'hash': '0x' + 'a' * 64, 'scanFrom': 99, 'scanThrough': cursor}
    if mortality:
        broadcast['mortality'] = {'period': 64, 'beforeSigning': 100,
                                 'afterSigning': 101, 'expiresAtOrBefore': 165}
    result = {'broadcast': broadcast}
    bh = '0x' + 'b' * 64
    finder = AsyncMock(return_value=None)
    if missing_archive:
        finder.side_effect = RuntimeError('archive unavailable')
    elif found:
        finder.return_value = SimpleNamespace(success=True, block_hash=bh, extrinsic_id=f'{cursor+1}-0001')
    substrate = SimpleNamespace(block_hash=AsyncMock(return_value=bh), find_extrinsic=finder)
    with sqlite3.connect(tmp_path / 'journal.db') as db:
        db.execute('CREATE TABLE weight_runs(id TEXT PRIMARY KEY,state TEXT,record TEXT,result TEXT)')
        db.execute('INSERT INTO weight_runs VALUES(?,?,?,?)', ('r', 'unknown', json.dumps(record), json.dumps(result)))
        db.commit()
        client = SimpleNamespace(_substrate=substrate)
        if missing_archive:
            with pytest.raises(RuntimeError, match='archive unavailable'):
                asyncio.run(recover(client, db, 'r', record, result, head))
            report = None
        else:
            report = asyncio.run(recover(client, db, 'r', record, result, head))
        state, saved = db.execute('SELECT state,result FROM weight_runs').fetchone()
    return state, json.loads(saved), report, finder


def test_expiry_requires_complete_finalized_scan(tmp_path):
    state, saved, report, finder = run_recovery(tmp_path, cursor=150)
    assert state == report['state'] == 'expired'
    assert saved['expiryEvidence']['expiresAtOrBefore'] == 165
    assert saved['expiryEvidence']['scannedThrough'] == 164
    assert finder.await_count == 14
    assert report['submittedTransaction'] is False


def test_expiry_cannot_skip_unscanned_blocks(tmp_path):
    state, saved, report, finder = run_recovery(tmp_path)
    assert state == 'unknown'
    assert saved['broadcast']['scanThrough'] == 118
    assert finder.await_count == 20


def test_not_expired_at_earlier_finalized_head(tmp_path):
    state, saved, report, finder = run_recovery(tmp_path, head=164, cursor=160)
    assert state == 'unknown'
    assert 'expiryEvidence' not in saved


def test_no_assumed_expiry_for_legacy_journal(tmp_path):
    state, saved, report, finder = run_recovery(tmp_path, cursor=150, mortality=False)
    assert state == 'unknown'
    assert 'expiryEvidence' not in saved


def test_archive_failure_never_unlocks_submission(tmp_path):
    state, saved, report, finder = run_recovery(tmp_path, cursor=150, missing_archive=True)
    assert state == 'unknown'
    assert 'expiryEvidence' not in saved


def test_included_transaction_wins_over_expiry(tmp_path):
    state, saved, report, finder = run_recovery(tmp_path, cursor=150, found=True)
    assert state == 'pending_reveal'
    assert saved['recoveredFromFinalizedChain'] is True
    assert 'expiryEvidence' not in saved


def test_previously_completed_scan_can_resolve_expiry(tmp_path):
    state, saved, report, finder = run_recovery(tmp_path, cursor=169)
    assert state == 'expired'
    assert finder.await_count == 0
