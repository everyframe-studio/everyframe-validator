import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from everyframe_validator.core.submission import definitely_not_included, submit_and_confirm


def expired_row():
    tx = '0x' + 'a' * 64
    return {'state': 'expired', 'record': {'block': 3980}, 'result': {
        'broadcast': {'hash': tx, 'scanFrom': 3979, 'scanThrough': 4043,
                      'mortality': {'period': 64, 'beforeSigning': 3980,
                                    'afterSigning': 3980, 'expiresAtOrBefore': 4044}},
        'expiryEvidence': {'reason': 'mortal_transaction_expired_without_inclusion',
                           'transactionHash': tx, 'scannedFrom': 3979, 'scannedThrough': 4043,
                           'finalizedBlock': 4044, 'finalizedBlockHash': '0x' + 'b' * 64,
                           'expiresAtOrBefore': 4044}}}


def test_only_proven_expiry_releases_slot():
    row = expired_row()
    assert definitely_not_included(row)
    for key, value in [('scannedThrough', 4042), ('finalizedBlock', 4043),
                       ('transactionHash', '0x' + 'c' * 64)]:
        invalid = expired_row()
        invalid['result']['expiryEvidence'][key] = value
        assert not definitely_not_included(invalid)
    assert not definitely_not_included({'state': 'expired', 'result': {}})
    row['state'] = 'unknown'
    assert not definitely_not_included(row)


@pytest.mark.parametrize('receipt', [True, False])
def test_one_broadcast_then_finalized_hash_confirmation(tmp_path, receipt):
    record = {'block': 100, 'commitReveal': True}
    tx = '0x' + 'a' * 64
    bh = '0x' + 'b' * 64
    stored = {'broadcast': {'hash': tx, 'scanFrom': 99, 'scanThrough': 98}}
    outcome = SimpleNamespace(success=True, block_hash=None, extrinsic_id=None)
    found = SimpleNamespace(success=True, block_hash=bh, extrinsic_id='101-0001') if receipt else None
    substrate = SimpleNamespace(submit_signed=AsyncMock(return_value=outcome),
        block_hash=AsyncMock(return_value=bh), find_extrinsic=AsyncMock(side_effect=[None, None, found]),
        raw=SimpleNamespace(get_chain_finalised_head=AsyncMock(return_value=bh),
                            get_block_number=AsyncMock(return_value=101)))
    with sqlite3.connect(tmp_path / 'journal.db') as db:
        db.execute('CREATE TABLE weight_runs(id TEXT PRIMARY KEY,state TEXT,record TEXT,result TEXT)')
        db.execute('INSERT INTO weight_runs VALUES(?,?,?,?)', ('r', 'submitting', json.dumps(record), json.dumps(stored)))
        db.commit()
        result = asyncio.run(submit_and_confirm(SimpleNamespace(_substrate=substrate),
            'signed', 'signer', db, 'r', record, timeout_seconds=1 if receipt else 0))
        state, raw = db.execute('SELECT state,result FROM weight_runs').fetchone()
    substrate.submit_signed.assert_awaited_once_with(
        'signed', 'signer', wait_for_inclusion=False, wait_for_finalization=False)
    if receipt:
        assert result.success and result.extrinsic_id == '101-0001'
        assert state == 'pending_reveal'
        assert json.loads(raw)['recoveredFromFinalizedChain']
    else:
        assert result is None and state == 'submitting'
        assert json.loads(raw)['broadcastAccepted'] is True


def test_rpc_rejection_is_not_rebroadcast(tmp_path):
    outcome = SimpleNamespace(success=False, block_hash=None, extrinsic_id=None)
    substrate = SimpleNamespace(submit_signed=AsyncMock(return_value=outcome))
    assert asyncio.run(submit_and_confirm(SimpleNamespace(_substrate=substrate),
        'signed', 'signer', None, 'r', {})) is outcome
    assert substrate.submit_signed.await_count == 1
