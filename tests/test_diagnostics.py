import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.exceptions import InvalidSignature

from everyframe_validator import runner
from everyframe_validator.core.submission import recover
from everyframe_validator.diagnostics import diagnostic
from everyframe_validator.doctor import doctor
from everyframe_validator.state import (
    locked,
    status_snapshot,
    journal_rows,
    atomic_json,
)
from test_runner import authorize, wire


def test_status_cli_works_during_exclusive_operation(profile):
    root, c = profile
    atomic_json(root / "status.json", {"state": "waiting", "atMs": 1})
    with locked(root):
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "everyframe_validator",
                "status",
                "--state-dir",
                str(root),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
    assert proc.returncode == 0, proc.stdout
    out = json.loads(proc.stdout)
    assert out["diagnosticOnly"] and out["lastRun"]["state"] == "waiting"
    assert out["operationInProgress"] and out["state"] == "processing"
    assert out["lastRunAgeSeconds"] > 0


def test_pending_wait_is_successful_run_and_snapshot(profile, chain, monkeypatch):
    root, _ = profile
    monkeypatch.setattr(
        runner, "tick", AsyncMock(return_value={"state": "pending_reveal"})
    )
    assert asyncio.run(runner.run(root, once=True)) == 0
    chain.execute.assert_not_called()


def test_diagnostics_never_echo_unknown_errors():
    secret = "private-untrusted-provider-content"
    for exc in [
        ValueError(secret),
        RuntimeError(secret),
        TimeoutError(secret),
        FileNotFoundError(secret),
    ]:
        assert secret not in json.dumps(diagnostic(exc))
    assert diagnostic(InvalidSignature())["code"] == "invalid_feed_signature"
    assert (
        diagnostic(ValueError("validator has no permit"))["code"]
        == "validator_permit_missing"
    )
    assert (
        diagnostic(FileNotFoundError(), "authorization")["code"]
        == "authorization_missing"
    )


def test_doctor_is_readonly_and_works_under_lock(
    profile, chain, payload, signing, monkeypatch
):
    root, c = profile
    import everyframe_validator.doctor as module
    from everyframe_validator.publish import sign

    monkeypatch.setattr(
        module, "fetch", lambda *_: json.dumps(sign(payload, signing[0])).encode()
    )
    before = sorted(p.name for p in root.iterdir())
    with locked(root):
        report = asyncio.run(doctor(root))
    assert report["ok"] is True
    assert report["signingPrerequisitesOk"] is False  # No authorization.
    assert any(c.get("code") == "authorization_missing" for c in report["checks"])
    assert sorted(p.name for p in root.iterdir()) == before
    chain.execute.assert_not_called()
    chain.preview.assert_not_called()


def test_doctor_offline_does_not_construct_rpc(profile, chain, monkeypatch):
    import bittensor

    monkeypatch.setattr(bittensor, "Client", lambda **_: pytest.fail("offline RPC"))
    assert (
        asyncio.run(doctor(profile[0], offline=True))["networkChecksPerformed"] is False
    )


def test_durable_hash_exists_before_broadcast(
    profile, payload, signing, chain, monkeypatch
):
    root, c = profile
    authorize(root, c)
    wire(monkeypatch, payload, signing)

    async def disconnect(*args, **kwargs):
        rows = journal_rows(root)
        assert rows[0]["state"] == "submitting"
        assert rows[0]["result"]["broadcast"]["hash"] == args[0].extrinsic_hash
        raise TimeoutError("disconnect")

    chain.execute.side_effect = disconnect
    with pytest.raises(TimeoutError):
        asyncio.run(runner.tick(root, submit=True))
    assert journal_rows(root)[0]["state"] == "unknown"
    assert status_snapshot(root)["state"] == "blocked"


def test_rejected_policy_never_broadcasts(
    profile, payload, signing, chain, monkeypatch
):
    root, c = profile
    authorize(root, c)
    wire(monkeypatch, payload, signing)
    chain.preview.return_value = SimpleNamespace(ok=False)
    with pytest.raises(ValueError, match="submission_policy_rejected"):
        asyncio.run(runner.tick(root, submit=True))
    chain.execute.assert_not_called()
    assert journal_rows(root)[0]["state"] == "not_submitted"


@pytest.mark.parametrize(
    "success,commit,expected",
    [
        (True, True, "pending_reveal"),
        (True, False, "finalized"),
        (False, True, "chain_failed"),
    ],
)
def test_exact_hash_finalized_recovery(profile, success, commit, expected):
    root, _ = profile
    tx_hash, block_hash = "0x" + "a" * 64, "0x" + "b" * 64
    record = {"block": 100, "commitReveal": commit}
    result = {"broadcast": {"hash": tx_hash, "scanFrom": 99, "scanThrough": 98}}
    found = SimpleNamespace(
        success=success, block_hash=block_hash, extrinsic_id="100-0001"
    )
    substrate = SimpleNamespace(
        block_hash=AsyncMock(return_value=block_hash),
        find_extrinsic=AsyncMock(side_effect=[None, found]),
    )
    with sqlite3.connect(root / "journal.db") as db:
        db.execute(
            "INSERT INTO weight_runs VALUES(?,?,?,?)",
            ("run", "unknown", json.dumps(record), json.dumps(result)),
        )
        db.commit()
        report = asyncio.run(
            recover(
                SimpleNamespace(_substrate=substrate), db, "run", record, result, 105
            )
        )
        assert db.execute("SELECT state FROM weight_runs").fetchone()[0] == expected
    assert report["state"] == expected and report["submittedTransaction"] is False
    assert all(c.args[0] == tx_hash for c in substrate.find_extrinsic.call_args_list)


def test_no_match_is_bounded_and_cannot_unblock(profile):
    root, _ = profile
    record = {"block": 100, "commitReveal": True}
    result = {
        "broadcast": {"hash": "0x" + "a" * 64, "scanFrom": 100, "scanThrough": 99}
    }
    substrate = SimpleNamespace(
        block_hash=AsyncMock(return_value="0x" + "b" * 64),
        find_extrinsic=AsyncMock(return_value=None),
    )
    with sqlite3.connect(root / "journal.db") as db:
        db.execute(
            "INSERT INTO weight_runs VALUES(?,?,?,?)",
            ("run", "unknown", json.dumps(record), json.dumps(result)),
        )
        db.commit()
        report = asyncio.run(
            recover(
                SimpleNamespace(_substrate=substrate), db, "run", record, result, 500
            )
        )
        assert db.execute("SELECT state FROM weight_runs").fetchone()[0] == "unknown"
        assert (
            json.loads(db.execute("SELECT result FROM weight_runs").fetchone()[0])[
                "broadcast"
            ]["scanThrough"]
            == 119
        )
    assert substrate.find_extrinsic.await_count == 20
    assert report["state"] == "blocked"


def test_legacy_unknown_keeps_journal(profile):
    root, _ = profile
    with sqlite3.connect(root / "journal.db") as db:
        report = asyncio.run(recover(None, db, "legacy", {}, {}, 500))
    assert report["reason"] == "transaction_hash_unavailable"


def test_second_runner_does_not_overwrite_status(profile):
    root, _ = profile
    atomic_json(root / "status.json", {"state": "pending_reveal", "atMs": 123})
    with locked(root):
        assert asyncio.run(runner.run(root, once=True)) == 2
    assert status_snapshot(root)["lastRun"]["state"] == "pending_reveal"


@pytest.mark.parametrize("kind", ["wrong_block", "wrong_id", "not_boolean"])
def test_invalid_recovery_evidence_stays_unknown(profile, kind):
    root, _ = profile
    record = {"block": 100, "commitReveal": True}
    result = {
        "broadcast": {"hash": "0x" + "a" * 64, "scanFrom": 100, "scanThrough": 99}
    }
    block_hash = "0x" + "b" * 64
    found = SimpleNamespace(success=True, block_hash=block_hash, extrinsic_id="100-1")
    if kind == "wrong_block":
        found.block_hash = "0x" + "c" * 64
    if kind == "wrong_id":
        found.extrinsic_id = "101-1"
    if kind == "not_boolean":
        found.success = "true"
    substrate = SimpleNamespace(
        block_hash=AsyncMock(return_value=block_hash),
        find_extrinsic=AsyncMock(return_value=found),
    )
    with sqlite3.connect(root / "journal.db") as db:
        db.execute(
            "INSERT INTO weight_runs VALUES(?,?,?,?)",
            ("run", "unknown", json.dumps(record), json.dumps(result)),
        )
        db.commit()
        with pytest.raises(ValueError, match="invalid_recovery_evidence"):
            asyncio.run(
                recover(
                    SimpleNamespace(_substrate=substrate),
                    db,
                    "run",
                    record,
                    result,
                    100,
                )
            )
        assert db.execute("SELECT state FROM weight_runs").fetchone()[0] == "unknown"


def test_recovery_rpc_error_keeps_durable_cursor(profile):
    root, _ = profile
    record = {"block": 100, "commitReveal": True}
    result = {
        "broadcast": {"hash": "0x" + "a" * 64, "scanFrom": 100, "scanThrough": 99}
    }
    substrate = SimpleNamespace(
        block_hash=AsyncMock(return_value="0x" + "b" * 64),
        find_extrinsic=AsyncMock(side_effect=[None, TimeoutError()]),
    )
    with sqlite3.connect(root / "journal.db") as db:
        db.execute(
            "INSERT INTO weight_runs VALUES(?,?,?,?)",
            ("run", "unknown", json.dumps(record), json.dumps(result)),
        )
        db.commit()
        with pytest.raises(TimeoutError):
            asyncio.run(
                recover(
                    SimpleNamespace(_substrate=substrate),
                    db,
                    "run",
                    record,
                    result,
                    110,
                )
            )
        state, saved = db.execute("SELECT state,result FROM weight_runs").fetchone()
        assert (
            state == "unknown" and json.loads(saved)["broadcast"]["scanThrough"] == 99
        )


def test_uncertain_tick_recovers_without_resubmitting(
    profile, payload, signing, chain, monkeypatch
):
    root, c = profile
    authorize(root, c)
    wire(monkeypatch, payload, signing)
    chain.execute.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        asyncio.run(runner.tick(root, submit=True))
    import bittensor as bt

    original = bt.Client

    def client(**kwargs):
        instance = original(**kwargs)
        instance._substrate.find_extrinsic = AsyncMock(
            return_value=SimpleNamespace(
                success=True, block_hash="0x" + "a" * 64, extrinsic_id="3999-0"
            )
        )
        return instance

    monkeypatch.setattr(bt, "Client", client)
    result = asyncio.run(runner.tick(root, only_reconcile=True))
    assert result["state"] == "pending_reveal"
    assert journal_rows(root)[0]["state"] == "pending_reveal"
    assert result["reports"][0]["reason"] == "exact_transaction_finalized"
    assert chain.execute.await_count == 1


def test_missing_state_is_actionable(tmp_path):
    report = asyncio.run(doctor(tmp_path / "missing", offline=True))
    assert report["ok"] is False
    assert report["checks"][0]["code"] == "required_file_missing"


def test_cancellation_persists_diagnostic(profile, monkeypatch):
    async def cancelled(*args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(runner, "_tick", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.tick(profile[0]))
    assert (
        status_snapshot(profile[0])["lastRun"]["code"]
        == "network_timeout_or_disconnect"
    )
