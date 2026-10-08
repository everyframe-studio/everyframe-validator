import asyncio
from copy import deepcopy
import json
import sqlite3
import time
from unittest.mock import AsyncMock

import pytest

from everyframe_validator import runner
from everyframe_validator.core.chain_scope import scope
from everyframe_validator.publish import sign
from everyframe_validator.feed import FeedUnavailable
from everyframe_validator.state import atomic_json, journal_rows, load_config, locked
from conftest import hot


def authorize(root,c,maximum=3):
    atomic_json(root/"authorization.json",{**scope(c["network"]),"action":"submit-reward-weights",
        "validatorHotkey":c["validatorHotkey"],"versionKey":c["versionKey"],"expiresAt":int(time.time()*1000)+86400000,
        "maxSubmissions":maximum,"id":"fixture-consent"})


def wire(monkeypatch,payload,signing):
    monkeypatch.setattr(runner,"fetch",lambda *args:json.dumps(sign(payload,signing[0])).encode())


def test_full_dry_run_does_not_sign(profile,payload,signing,chain,monkeypatch):
    root,c=profile
    wire(monkeypatch,payload,signing)
    result=asyncio.run(runner.tick(root))
    assert result["state"]=="dry_run"
    assert result["allBurn"] is False
    chain.execute.assert_not_called()
    assert journal_rows(root)==[]


def test_explicit_submit_uses_external_hotkey_and_journals(profile,payload,signing,chain,monkeypatch):
    root,c=profile
    authorize(root,c)
    wire(monkeypatch,payload,signing)
    result=asyncio.run(runner.tick(root,submit=True))
    assert result["state"]=="pending_reveal"
    assert result["submittedTransaction"]
    assert chain.execute.await_count==1
    assert chain.execute.call_args.kwargs["retries"]==0
    assert journal_rows(root)[0]["record"]["validatorHotkey"]==hot(1)
    monkeypatch.setattr(runner,"reconcile",AsyncMock(return_value=[]))
    assert asyncio.run(runner.tick(root,submit=True))["state"]=="blocked"
    assert chain.execute.await_count==1


def test_missing_consent_never_signs(profile,payload,signing,chain,monkeypatch):
    root,c=profile
    wire(monkeypatch,payload,signing)
    with pytest.raises(FileNotFoundError): asyncio.run(runner.tick(root,submit=True))
    chain.execute.assert_not_called()


def test_ambiguous_broadcast_survives_restart(profile,payload,signing,chain,monkeypatch):
    root,c=profile
    authorize(root,c)
    wire(monkeypatch,payload,signing)
    chain.execute.side_effect=TimeoutError("test disconnected")
    with pytest.raises(TimeoutError):asyncio.run(runner.tick(root,submit=True))
    assert journal_rows(root)[0]["state"]=="unknown"
    assert asyncio.run(runner.tick(root,submit=True))["state"]=="blocked"
    assert chain.execute.await_count==1


def test_cancelled_broadcast_is_unknown(profile,payload,signing,chain,monkeypatch):
    root,c=profile
    authorize(root,c)
    wire(monkeypatch,payload,signing)
    chain.execute.side_effect=asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):asyncio.run(runner.tick(root,submit=True))
    assert journal_rows(root)[0]["state"]=="unknown"


def test_not_published_waits_without_burn(profile,chain,monkeypatch):
    def unavailable(*args):raise FeedUnavailable()
    monkeypatch.setattr(runner,"fetch",unavailable)
    assert asyncio.run(runner.tick(profile[0]))["state"]=="waiting"
    chain.execute.assert_not_called()


def test_conflicting_bindings_are_not_accepted(profile,payload,signing,chain,monkeypatch):
    root,c=profile
    wire(monkeypatch,payload,signing)
    asyncio.run(runner.tick(root))
    payload["bindings"]["a"]=hot(5)
    with pytest.raises(ValueError,match="immutable_file_conflict"):
        asyncio.run(runner.tick(root))


def test_wallet_mismatch_never_signs(profile,payload,signing,chain,monkeypatch):
    root,c=profile
    authorize(root,c)
    wire(monkeypatch,payload,signing)
    chain.wallet.hotkeypub.ss58_address=hot(4)
    with pytest.raises(ValueError,match="wallet does not match"):
        asyncio.run(runner.tick(root,submit=True))
    chain.execute.assert_not_called()


def test_identity_and_missing_journal_rejected(profile):
    root,c=profile
    c["validatorHotkey"]=hot(4)
    atomic_json(root/"config.json",c)
    with pytest.raises(ValueError,match="identity_changed"):load_config(root)


def test_exclusive_process_lock(profile):
    with locked(profile[0]):
        with pytest.raises(BlockingIOError):
            with locked(profile[0]):pass


def test_mainnet_nonowner_signed_flow_is_scoped(tmp_path,signing,chain,monkeypatch):
    from everyframe_validator.feed import PROFILES
    from everyframe_validator.publish import project
    from everyframe_validator.state import initialize
    from conftest import artifact
    c={**PROFILES["mainnet"],"publicKey":signing[1],"validatorHotkey":hot(1),"wallet":"fixture"}
    root=tmp_path/"mainnet"
    initialize(root,c)
    authorize(root,c)
    chain.network["name"]="finney"
    chain.params["weights_version"]=1030
    payload=project(artifact("finney"),{"a":hot(2),"b":hot(3)})
    wire(monkeypatch,payload,signing)
    result=asyncio.run(runner.tick(root,submit=True))
    assert result["state"]=="pending_reveal"
    assert result["netuid"]==117
    record=journal_rows(root)[0]["record"]
    assert record["validatorHotkey"]==hot(1)
    assert record["plan"]["burnHotkey"]==hot(0)
    assert record["network"]=="finney"
    assert record["authorizationId"]=="fixture-consent"


def test_expired_consent_fails_before_downloading(profile,payload,signing,chain,monkeypatch):
    root,c=profile
    authorize(root,c)
    consent=json.loads((root/"authorization.json").read_text())
    consent["expiresAt"]=0
    atomic_json(root/"authorization.json",consent)
    with pytest.raises(ValueError,match="expired"):
        asyncio.run(runner.tick(root,submit=True))
    chain.execute.assert_not_called()


def test_future_finalization_never_signs(profile,payload,signing,chain,monkeypatch):
    root,c=profile
    authorize(root,c)
    payload["artifact"]["finalizedBlock"]=5000
    wire(monkeypatch,payload,signing)
    with pytest.raises(ValueError,match="future finalized"):
        asyncio.run(runner.tick(root,submit=True))
    chain.execute.assert_not_called()
