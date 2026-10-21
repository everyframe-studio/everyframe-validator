"""Read-only public-metadata preflight; never unlock, create or replace a key."""

from pathlib import Path
import os
import stat
import json

MAX_KEYFILE_BYTES = 64_000
PUBLIC_FIELDS = {"cryptoType", "publicKey", "accountId", "ss58Address"}


class WalletConfigurationError(ValueError):
    def __init__(self, code, paths=None):
        super().__init__(code)
        self.paths = paths or {}


def wallet_paths(wallet):
    return {
        "walletPath": str(Path(wallet.path).expanduser().absolute()),
        "hotkeyFile": str(Path(wallet.hotkey_file.path).expanduser().absolute()),
        "publicHotkeyFile": str(Path(wallet.hotkeypub_file.path).expanduser().absolute()),
    }


def regular_file(path):
    try:
        return stat.S_ISREG(Path(path).stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False


def public_metadata(path):
    """Read a bounded existing keyfile and construct a PUBLIC-ONLY keypair.

    Legacy hotkeys contain public fields alongside their private fields. Never
    pass the document or any secret fields to a keypair deserializer, never
    decrypt, and never include the raw file in diagnostics.
    """
    from bittensor.keyfiles import Keypair, keyfile_data_is_encrypted
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_KEYFILE_BYTES:
            raise ValueError("invalid_public_metadata")
        raw = file.read(MAX_KEYFILE_BYTES + 1)
    if len(raw) > MAX_KEYFILE_BYTES:
        raise ValueError("invalid_public_metadata")
    if keyfile_data_is_encrypted(raw):
        return None, True

    def public_fields(pairs):
        result = {}
        for name, value in pairs:
            if name in PUBLIC_FIELDS:
                if name in result:
                    raise ValueError("duplicate_public_metadata")
                result[name] = value
        return result

    try:
        data = json.loads(raw, object_pairs_hook=public_fields)
    except (json.JSONDecodeError, UnicodeError):
        return None, False
    if not isinstance(data, dict):
        return None, False
    kind = data.get("cryptoType", 1)  # Legacy Bittensor keys default to sr25519.
    if type(kind) is not int or kind not in (0, 1):
        raise ValueError("invalid_public_metadata")
    public_values = [data[name] for name in ("publicKey", "accountId") if data.get(name)]
    address = data.get("ss58Address")
    if not public_values and not address:
        return None, False
    pair = None
    for value in public_values:
        if not isinstance(value, str) or len(value.removeprefix("0x")) != 64:
            raise ValueError("invalid_public_metadata")
        candidate = Keypair(public_key=bytes.fromhex(value.removeprefix("0x")),
                            crypto_type=kind, ss58_format=42)
        if pair is not None and candidate.public_key != pair.public_key:
            raise ValueError("invalid_public_metadata")
        pair = candidate
    if address:
        if not isinstance(address, str):
            raise ValueError("invalid_public_metadata")
        candidate = Keypair(ss58_address=address, crypto_type=kind, ss58_format=42)
        if pair is not None and candidate.public_key != pair.public_key:
            raise ValueError("invalid_public_metadata")
        pair = candidate
    return pair.ss58_address, False


def wallet_identity(wallet):
    address, encrypted = public_metadata(wallet.hotkey_file.path)
    if address is not None:
        return {"address": address, "publicIdentitySource": "hotkey_metadata",
                "publicIdentityVerified": True}
    # Sidecars remain an optional source for encrypted/metadata-less wallets,
    # not a mandatory Bittensor wallet format and never a password prompt.
    if regular_file(wallet.hotkeypub_file.path):
        address, _ = public_metadata(wallet.hotkeypub_file.path)
        if address is not None:
            return {"address": address, "publicIdentitySource": "public_sidecar",
                    "publicIdentityVerified": True}
    if encrypted:
        return {"address": None, "publicIdentitySource": "encrypted_hotkey",
                "publicIdentityVerified": False}
    raise ValueError("public_metadata_unavailable")


def checked_wallet(config, *, with_identity=False):
    """Check file presence and public metadata, without unlocking a private key.

    walletPath is a parent containing wallet directories. A likely correction is
    only a diagnostic hint, never an automatic configuration or identity change.
    The signing path must still unlock and check the actual signer separately.
    """
    if not config.get("wallet"):
        raise ValueError("wallet_not_configured")
    import bittensor as bt

    path = config.get("walletPath")
    if path is not None:
        if not isinstance(path, str) or not path or "\x00" in path or any(ord(c) < 32 for c in path):
            raise WalletConfigurationError("invalid_wallet_path")
        path = str(Path(path).expanduser())
        if not Path(path).is_absolute():
            raise WalletConfigurationError("invalid_wallet_path")
    try:
        wallet = bt.Wallet(config["wallet"], config.get("walletHotkey", "default"),
                           **({"path": path} if path is not None else {}))
    except (ValueError, TypeError):
        raise WalletConfigurationError("invalid_wallet_configuration") from None
    paths = wallet_paths(wallet)
    try:
        private_exists = regular_file(paths["hotkeyFile"])
        root = Path(paths["walletPath"])
        if not private_exists and root.name == wallet.name:
            candidate = bt.Wallet(wallet.name, wallet.hotkey_str, path=str(root.parent))
            # Confirm a matching public identity before suggesting another root.
            # Only public metadata is used; no key unlock or signing.
            try:
                if (regular_file(candidate.hotkey_file.path)
                        and wallet_identity(candidate)["address"] == config["validatorHotkey"]):
                    raise WalletConfigurationError("wallet_path_includes_name",
                                                   {**paths, "suggestedWalletPath": str(root.parent)})
            except WalletConfigurationError:
                raise
            except Exception:
                pass  # An unreadable/nonmatching alternative is never selected.
        if not private_exists:
            raise WalletConfigurationError("wallet_hotkey_missing", paths)
        if not os.access(paths["hotkeyFile"], os.R_OK):
            raise WalletConfigurationError("wallet_file_unreadable", paths)
    except PermissionError:
        raise WalletConfigurationError("wallet_file_unreadable", paths) from None
    try:
        identity = wallet_identity(wallet)
    except FileNotFoundError:
        raise WalletConfigurationError("wallet_hotkey_missing", paths) from None
    except PermissionError:
        raise WalletConfigurationError("wallet_file_unreadable", paths) from None
    except Exception:
        raise WalletConfigurationError("wallet_public_metadata_invalid", paths) from None
    if identity["address"] is not None and identity["address"] != config["validatorHotkey"]:
        raise ValueError("wallet does not match intended validator")
    return (wallet, identity) if with_identity else wallet


def check_signer(signer, expected):
    """A public sidecar is not proof of the private signing key's identity."""
    if signer.ss58_address != expected:
        raise ValueError("wallet does not match intended validator")
    return signer
