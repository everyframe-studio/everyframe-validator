"""Operator CLI; keys stay local, signing requires explicit bounded consent."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time
import uuid

from . import __version__
from .core.chain_scope import scope
from .feed import PROFILES, public_key, validate_url
from .state import initialize, locked, load_config, atomic_json, status_snapshot
from .runner import run, tick
from .diagnostics import diagnostic
from .doctor import doctor


def parser():
    p = argparse.ArgumentParser(description="Everyframe independent validator. Mainnet SN117; dry-run by default.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Initialize private profile (no network calls or transactions)")
    init.add_argument("--network", choices=PROFILES, default="mainnet")
    init.add_argument("--validator-hotkey", required=True, help="Public SS58 hotkey, never a seed")
    init.add_argument("--wallet", help="Local Bittensor wallet name (needed only for submission)")
    init.add_argument("--wallet-hotkey", default="default")
    init.add_argument("--wallet-path", help="Wallet directory; mounted read-only in Docker")
    init.add_argument("--wallet-password-file", help="Owner-only JSON with hotkey password, not the seed")
    init.add_argument("--feed-url", help="Explicit trusted HTTPS feed override")
    init.add_argument("--public-key", help="Pinned Ed25519 DER/base64 accounting signer override")
    init.add_argument("--version-key", type=int)
    commands = {"init": init}
    for name, help_text in (("run", "Validate continuously; never signs without --submit"),
                            ("status", "Inspect local profile and submission states"),
                            ("doctor", "Check profile, journal, wallet identity, chain and feed without signing"),
                            ("reconcile", "Read chain evidence for submitted weights; never resubmit"),
                            ("authorize", "Grant YOUR validator local, expiring signing consent")):
        commands[name] = sub.add_parser(name, help=help_text)
    for item in commands.values():
        item.add_argument("--state-dir", required=True, help="Dedicated private directory for this network and hotkey")
    commands["run"].add_argument("--submit", action="store_true", help="Sign using your local wallet; also requires authorize")
    commands["run"].add_argument("--once", action="store_true")
    commands["run"].add_argument("--interval", type=int, default=60)
    commands["doctor"].add_argument("--offline", action="store_true", help="Skip RPC and feed checks")
    commands["authorize"].add_argument("--days", type=int, default=30)
    commands["authorize"].add_argument("--max-submissions", type=int, default=1000)
    commands["authorize"].add_argument("--yes", action="store_true", help="Acknowledge future run --submit can sign transactions and incur fees")
    return p


def main():
    os.umask(0o077)
    p = parser()
    args = p.parse_args()
    root = Path(args.state_dir).absolute()
    try:
        if args.command == "init":
            from bittensor.wallets import is_bittensor_address
            if not is_bittensor_address(args.validator_hotkey):
                raise ValueError("invalid_public_hotkey")
            profile = PROFILES[args.network]
            config = {**profile, "validatorHotkey": args.validator_hotkey, "wallet": args.wallet,
                      "walletHotkey": args.wallet_hotkey, "walletPath": args.wallet_path,
                      "walletPasswordFile": args.wallet_password_file}
            if args.feed_url:
                config["feed"] = args.feed_url
            if args.public_key:
                config["publicKey"] = args.public_key
            if args.version_key is not None:
                if args.version_key < profile["versionKey"]:
                    raise ValueError("outdated_version_key")
                config["versionKey"] = args.version_key
            validate_url(config["feed"])
            public_key(config["publicKey"])
            initialize(root, config)
            result = {"state": "initialized", "netuid": config["netuid"], "signingEnabled": False,
                      "message": "Profile ready. Feed availability and validator permit are checked during run."}
        elif args.command == "authorize":
            if not args.yes or not 1 <= args.days <= 365 or not 1 <= args.max_submissions <= 1000000:
                p.error("authorize requires --yes, --days 1..365 and --max-submissions 1..1000000")
            with locked(root) as directory:
                c = load_config(directory)
                if not c.get("wallet"):
                    raise ValueError("wallet_not_configured")
                consent = {**scope(c["network"], c["netuid"]), "action": "submit-reward-weights",
                           "validatorHotkey": c["validatorHotkey"], "versionKey": c["versionKey"],
                           "expiresAt": int(time.time()*1000) + args.days * 86400000,
                           "maxSubmissions": args.max_submissions, "id": str(uuid.uuid4())}
                atomic_json(directory / "authorization.json", consent)
            result = {"state": "authorized_locally", "expiresAt": consent["expiresAt"],
                      "maxSubmissions": args.max_submissions, "submittedTransaction": False,
                      "message": "Only run --submit can sign. This is your authorization, not approval from the subnet owner."}
        elif args.command == "status":
            result = status_snapshot(root)
        elif args.command == "doctor":
            result = asyncio.run(asyncio.wait_for(doctor(root, offline=args.offline), timeout=60))
        elif args.command == "reconcile":
            result = asyncio.run(asyncio.wait_for(tick(root, only_reconcile=True), timeout=300))
        else:
            if not 15 <= args.interval <= 3600:
                p.error("interval must be 15..3600 seconds")
            raise SystemExit(asyncio.run(run(root, submit=args.submit, once=args.once, interval=args.interval)))
        print(json.dumps(result))
        if args.command == "doctor" and not result["ok"]:
            raise SystemExit(2)
        if args.command == "reconcile" and result["state"] == "blocked":
            raise SystemExit(2)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(json.dumps(diagnostic(exc)))
        raise SystemExit(2)
