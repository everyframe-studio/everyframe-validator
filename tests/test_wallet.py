"""Pinned SDK paths and legacy public metadata; no real keys or transactions."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import bittensor as bt
from bittensor.keyfiles import Keypair, serialized_keypair_to_keyfile_data
import pytest

from conftest import hot
from everyframe_validator import cli
from everyframe_validator.diagnostics import diagnostic
from everyframe_validator.doctor import doctor
from everyframe_validator.feed import PROFILES
from everyframe_validator.state import initialize
from everyframe_validator.wallet import (
    WalletConfigurationError, checked_wallet, check_signer, wallet_paths,
)


@pytest.fixture
def real_wallet(tmp_path, monkeypatch):
    root = tmp_path / "wallets"
    keydir = root / "default" / "hotkeys"
    keydir.mkdir(parents=True)
    private = keydir / "default"
    public = keydir / "defaultpub.txt"
    metadata = json.loads(bytes(serialized_keypair_to_keyfile_data(Keypair(ss58_address=hot(1)))))
    public.write_text(json.dumps(metadata))
    private.write_text(json.dumps({**metadata, "secretSeed": "SECRET_SENTINEL_NEVER_UNLOCKED",
                                  "secretPhrase": "SECRET_SENTINEL_NEVER_PRINTED"}))
    monkeypatch.setattr(bt.Wallet, "get_hotkey", lambda *_: pytest.fail("private key unlock"))
    return {**PROFILES["mainnet"], "validatorHotkey": hot(1), "wallet": "default",
            "walletHotkey": "default", "walletPath": str(root), "walletPasswordFile": None}, private, public


def test_real_sdk_parent_resolves_without_deserializing_private_key(real_wallet, monkeypatch):
    config, private, _ = real_wallet
    from bittensor.keyfiles import Keyfile
    monkeypatch.setattr(Keyfile, "get_keypair", lambda *_: pytest.fail("keyfile private-key deserialization"))
    paths = wallet_paths(checked_wallet(config))
    assert paths["hotkeyFile"] == str(private)
    assert paths["publicHotkeyFile"] == str(private) + "pub.txt"


def test_duplicate_wallet_root_is_reported_without_repair(real_wallet):
    config, private, public = real_wallet
    config["walletPath"] += "/default"
    before = {p: p.read_bytes() for p in (private, public)}
    with pytest.raises(WalletConfigurationError) as error:
        checked_wallet(config)
    out = diagnostic(error.value)
    assert out["code"] == "wallet_path_includes_name"
    assert out["walletPaths"]["suggestedWalletPath"] == str(private.parents[2])
    assert out["walletPaths"]["hotkeyFile"].endswith("/default/default/hotkeys/default")
    assert config["walletPath"].endswith("/default")
    assert {p: p.read_bytes() for p in before} == before


def test_missing_private_hotkey_is_distinguished(real_wallet):
    real_wallet[1].unlink()
    with pytest.raises(WalletConfigurationError) as error:
        checked_wallet(real_wallet[0])
    assert diagnostic(error.value)["code"] == "wallet_hotkey_missing"
    assert diagnostic(error.value)["walletPaths"]["hotkeyFile"] == str(real_wallet[1])


def test_legacy_private_only_wallet_does_not_generate_public_sidecar(real_wallet):
    config, private, public = real_wallet
    public.unlink()
    before = private.read_bytes()
    _, identity = checked_wallet(config, with_identity=True)
    assert identity["publicIdentitySource"] == "hotkey_metadata"
    assert identity["publicIdentityVerified"] and identity["address"] == config["validatorHotkey"]
    assert not public.exists() and private.read_bytes() == before


@pytest.mark.parametrize("fields", [("ss58Address",), ("publicKey",), ("accountId",),
                                  ("publicKey", "accountId", "ss58Address")])
def test_legacy_public_field_variants_without_sidecar(real_wallet, fields):
    config, private, public = real_wallet
    metadata = json.loads(private.read_text())
    private.write_text(json.dumps({k: metadata[k] for k in (*fields, "secretSeed", "secretPhrase")}))
    public.unlink()
    assert checked_wallet(config, with_identity=True)[1]["publicIdentitySource"] == "hotkey_metadata"


def test_public_identity_must_match(real_wallet):
    config, _, _ = real_wallet
    config["validatorHotkey"] = hot(2)
    with pytest.raises(ValueError, match="wallet does not match"):
        checked_wallet(config)


def test_nonmatching_alternative_is_not_suggested(real_wallet):
    config, _, _ = real_wallet
    config["walletPath"] += "/default"
    config["validatorHotkey"] = hot(2)
    with pytest.raises(WalletConfigurationError) as error:
        checked_wallet(config)
    assert error.value.args[0] == "wallet_hotkey_missing"
    assert "suggestedWalletPath" not in error.value.paths


def test_intentional_nested_wallet_layout_is_not_rewritten(real_wallet):
    config, _, public = real_wallet
    config["walletPath"] += "/default"
    nested = Path(config["walletPath"]) / "default" / "hotkeys"
    nested.mkdir(parents=True)
    (nested / "default").write_bytes(b"metadata only")
    (nested / "defaultpub.txt").write_bytes(public.read_bytes())
    assert wallet_paths(checked_wallet(config))["hotkeyFile"] == str(nested / "default")
    assert config["walletPath"].endswith("/default")


@pytest.mark.parametrize("path", ["wallets", "", "bad\npath", "bad\x00path"])
def test_invalid_roots_are_rejected_without_creation(real_wallet, path):
    config, _, _ = real_wallet
    config["walletPath"] = path
    with pytest.raises(WalletConfigurationError, match="invalid_wallet_path"):
        checked_wallet(config)


def test_public_metadata_errors_do_not_leak_secrets(real_wallet):
    config, private, public = real_wallet
    private.write_text(json.dumps({"publicKey": "invalid", "secretSeed": "private-untrusted-content"}))
    public.unlink()
    with pytest.raises(WalletConfigurationError) as error:
        checked_wallet(config)
    out = diagnostic(error.value)
    assert out["code"] == "wallet_public_metadata_invalid"
    assert "private-untrusted-content" not in json.dumps(out)


def test_unreadable_private_file_is_caught_without_opening(real_wallet, monkeypatch):
    import os
    config, private, _ = real_wallet
    original = os.access
    monkeypatch.setattr(os, "access", lambda path, mode: False if Path(path) == private else original(path, mode))
    with pytest.raises(WalletConfigurationError, match="wallet_file_unreadable"):
        checked_wallet(config)


def test_encrypted_hotkey_without_sidecar_defers_identity_without_unlock(real_wallet, monkeypatch):
    import bittensor.keyfiles as keyfiles
    from bittensor.keyfiles import Keyfile
    config, private, public = real_wallet
    private.write_bytes(b"ENCRYPTED_FIXTURE")
    public.unlink()
    monkeypatch.setattr(keyfiles, "keyfile_data_is_encrypted", lambda data: data == b"ENCRYPTED_FIXTURE")
    monkeypatch.setattr(Keyfile, "get_keypair", lambda *_: pytest.fail("keyfile unlock"))
    _, identity = checked_wallet(config, with_identity=True)
    assert identity == {"address": None, "publicIdentitySource": "encrypted_hotkey",
                        "publicIdentityVerified": False}


def test_encrypted_hotkey_can_use_optional_public_sidecar(real_wallet, monkeypatch):
    import bittensor.keyfiles as keyfiles
    config, private, _ = real_wallet
    private.write_bytes(b"ENCRYPTED_FIXTURE")
    monkeypatch.setattr(keyfiles, "keyfile_data_is_encrypted", lambda data: data == b"ENCRYPTED_FIXTURE")
    identity = checked_wallet(config, with_identity=True)[1]
    assert identity["publicIdentityVerified"] and identity["publicIdentitySource"] == "public_sidecar"


def test_private_public_field_conflicts_are_rejected(real_wallet):
    config, private, _ = real_wallet
    metadata = json.loads(private.read_text())
    metadata["ss58Address"] = hot(2)
    private.write_text(json.dumps(metadata))
    with pytest.raises(WalletConfigurationError, match="wallet_public_metadata_invalid"):
        checked_wallet(config)


def test_broken_optional_sidecar_is_not_needed_for_valid_legacy_hotkey(real_wallet):
    config, _, public = real_wallet
    public.write_bytes(b"irrelevant malformed sidecar")
    assert checked_wallet(config, with_identity=True)[1]["publicIdentitySource"] == "hotkey_metadata"


def test_fifo_is_not_opened(real_wallet):
    import os
    config, private, _ = real_wallet
    private.unlink()
    os.mkfifo(private)
    with pytest.raises(WalletConfigurationError, match="wallet_hotkey_missing"):
        checked_wallet(config)


def test_legacy_wallet_doctor_never_creates_sidecar_or_changes_state(real_wallet, tmp_path):
    config, private, public = real_wallet
    public.unlink()
    state = tmp_path / "legacy-state"
    initialize(state, config)
    before = {p.name: p.read_bytes() for p in state.iterdir()}
    private_before = private.read_bytes()
    report = asyncio.run(doctor(state, offline=True))
    assert report["ok"] is True
    check = next(c for c in report["checks"] if c["name"] == "wallet")
    assert check["publicIdentitySource"] == "hotkey_metadata"
    assert "SECRET_SENTINEL" not in json.dumps(report)
    assert not public.exists() and private.read_bytes() == private_before
    assert {p.name: p.read_bytes() for p in state.iterdir()} == before


def test_private_signer_must_match_even_if_sidecar_matches():
    with pytest.raises(ValueError, match="wallet does not match"):
        check_signer(SimpleNamespace(ss58_address=hot(2)), hot(1))


@pytest.mark.parametrize("encrypted", [False, True])
def test_real_sdk_existing_signer_works_without_sidecar(tmp_path, monkeypatch, encrypted):
    # Publicly known fixture seed, never a real wallet or a chain transaction.
    key = Keypair.from_seed(bytes([17]) * 32)
    root = tmp_path / "fixture-wallets"
    wallet = bt.Wallet("fixture", "default", path=str(root))
    wallet.hotkey_file.set_keypair(key, encrypt=encrypted, password="fixture-password")
    config = {"wallet": "fixture", "walletHotkey": "default", "walletPath": str(root),
              "validatorHotkey": key.ss58_address}
    before = Path(wallet.hotkey_file.path).read_bytes()
    with monkeypatch.context() as guard:
        guard.setattr(bt.Wallet, "get_hotkey", lambda *_: pytest.fail("preflight unlock"))
        checked, identity = checked_wallet(config, with_identity=True)
    assert identity["publicIdentityVerified"] is not encrypted
    signer = checked.get_hotkey("fixture-password" if encrypted else None)
    assert check_signer(signer, config["validatorHotkey"]).ss58_address == key.ss58_address
    assert not Path(wallet.hotkeypub_file.path).exists()
    assert Path(wallet.hotkey_file.path).read_bytes() == before


def test_init_rejects_duplicate_before_creating_state(real_wallet, tmp_path, monkeypatch, capsys):
    config, _, _ = real_wallet
    state = tmp_path / "new-state"
    monkeypatch.setattr(sys, "argv", ["validator", "init", "--state-dir", str(state),
        "--validator-hotkey", config["validatorHotkey"], "--wallet", "default",
        "--wallet-path", config["walletPath"] + "/default"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert json.loads(capsys.readouterr().out)["code"] == "wallet_path_includes_name"
    assert not state.exists()


def test_init_accepts_valid_readonly_checked_wallet(real_wallet, tmp_path, monkeypatch, capsys):
    config, _, _ = real_wallet
    state = tmp_path / "new-state"
    monkeypatch.setattr(sys, "argv", ["validator", "init", "--state-dir", str(state),
        "--validator-hotkey", config["validatorHotkey"], "--wallet", "default",
        "--wallet-path", config["walletPath"]])
    cli.main()
    assert json.loads(capsys.readouterr().out)["state"] == "initialized"
    assert not (state / "authorization.json").exists()


def test_authorize_bad_profile_cannot_create_consent_or_rewrite_state(real_wallet, tmp_path, monkeypatch, capsys):
    config, _, _ = real_wallet
    config["walletPath"] += "/default"
    state = tmp_path / "existing"
    initialize(state, config)
    before = {p.name: p.read_bytes() for p in state.iterdir()}
    monkeypatch.setattr(sys, "argv", ["validator", "authorize", "--state-dir", str(state), "--yes"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert json.loads(capsys.readouterr().out)["code"] == "wallet_path_includes_name"
    assert {p.name: p.read_bytes() for p in state.iterdir()} == before


def test_doctor_marks_configured_wallet_failure_not_ok(real_wallet, tmp_path):
    config, _, _ = real_wallet
    config["walletPath"] += "/default"
    state = tmp_path / "existing"
    initialize(state, config)
    before = {p.name: p.read_bytes() for p in state.iterdir()}
    report = asyncio.run(doctor(state, offline=True))
    assert report["ok"] is False and report["readOnlyOk"] is True
    assert report["signingPrerequisitesOk"] is None
    check = next(c for c in report["checks"] if c["name"] == "wallet")
    assert check["required"] and check["code"] == "wallet_path_includes_name"
    assert {p.name: p.read_bytes() for p in state.iterdir()} == before


def test_readonly_profile_without_wallet_is_still_supported(tmp_path):
    state = tmp_path / "readonly"
    initialize(state, {**PROFILES["mainnet"], "validatorHotkey": hot(1), "wallet": None})
    report = asyncio.run(doctor(state, offline=True))
    assert report["ok"] and report["readOnlyOk"]


def test_doctor_cli_returns_failure_for_configured_bad_wallet(real_wallet, tmp_path, monkeypatch, capsys):
    config, _, _ = real_wallet
    config["walletPath"] += "/default"
    state = tmp_path / "existing"
    initialize(state, config)
    monkeypatch.setattr(sys, "argv", ["validator", "doctor", "--state-dir", str(state), "--offline"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["readOnlyOk"] is True
    assert any(c.get("code") == "wallet_path_includes_name" for c in out["checks"])
