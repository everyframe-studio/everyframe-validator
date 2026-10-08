"""Operator-side adapter: existing sealed epoch DB → immutable signed public JSON.

This is not run by external validators. Never exports customer-level evidence.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import stat

from cryptography.hazmat.primitives.serialization import load_pem_private_key, Encoding, PublicFormat
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .core.chain_scope import scope, check_database
from .core.reward_policy import canonical, digest, load_epoch
from .feed import ARTIFACT_KEYS, MAINNET_KEYS, WINDOW_KEYS, SCORE_KEYS, PRICE_KEYS, SCHEMA, PROFILES, strict_json, validate_payload
from .state import atomic_json


def project(artifact, bindings):
    keys = ARTIFACT_KEYS | (MAINNET_KEYS if artifact["network"] == "finney" else set())
    public = {key: artifact[key] for key in keys}
    public["window"] = {key: artifact["window"][key] for key in WINDOW_KEYS}
    public["window"]["scores"] = [{key: row[key] for key in SCORE_KEYS} for row in artifact["window"]["scores"]]
    public["priceSource"] = {key: artifact["priceSource"][key] for key in PRICE_KEYS if key in artifact["priceSource"]}
    payload = {"schema": SCHEMA, "artifact": public, "sourceHash": digest(artifact),
               "bindings": {row["minerId"]: bindings[row["minerId"]] for row in public["window"]["scores"]}}
    return validate_payload(payload, scope(artifact["network"], artifact["netuid"]), artifact["epoch"])


def sign(payload, key):
    return {"payload": payload, "signature": base64.b64encode(key.sign(canonical(payload).encode())).decode()}


def publish(args):
    profile = PROFILES[args.network]
    domain = scope(profile["network"], profile["netuid"])
    check_database(args.epochs, domain)
    a = load_epoch(args.epochs, domain["netuid"], args.epoch)
    bindings = strict_json(Path(args.bindings).read_bytes())
    payload = project(a, bindings)
    fd = os.open(args.signing_key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("signing_key_must_be_owner_only")
        key = load_pem_private_key(f.read(16385), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("ed25519_required")
    encoded = base64.b64encode(key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).decode()
    if encoded != (args.expected_public_key or profile["publicKey"]):
        raise ValueError("unexpected_publisher_key")
    out = Path(args.output).absolute()
    if out.is_symlink():
        raise ValueError("output_directory_symlink")
    out.mkdir(parents=True, exist_ok=True)
    target = out / (str(args.epoch) + ".json")
    atomic_json(target, sign(payload, key), immutable=True)
    target.chmod(0o644)
    return {"state": "published_locally", "epoch": args.epoch, "netuid": domain["netuid"],
            "payloadHash": digest(payload), "publicFile": str(target), "chainTransactions": 0}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--network", choices=PROFILES, default="mainnet")
    for name in ("epochs", "bindings", "signing-key", "output"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--epoch", type=int, required=True)
    p.add_argument("--expected-public-key", help="Explicit reviewed signer override; must match validator profiles")
    args = p.parse_args()
    try:
        print(json.dumps(publish(args)))
    except Exception as exc:
        print(json.dumps({"state": "failed", "errorType": type(exc).__name__, "message": "No epoch published. Check sealed accounting, bindings, key permissions and immutable conflicts."}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
