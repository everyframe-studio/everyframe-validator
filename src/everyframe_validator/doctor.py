"""Read-only readiness checks, also usable while the signing loop owns its lock."""

from .core.chain_scope import scope, check_authorization, read_network
from .diagnostics import diagnostic
from .feed import fetch, verify, FeedUnavailable
from .state import (
    private_directory,
    load_config,
    read_private,
    status_snapshot,
    journal_rows,
)
import asyncio


async def doctor(path, offline=False):
    checks = []

    def failure(name, exc, required=True):
        checks.append(
            {"name": name, "ok": False, "required": required, **diagnostic(exc, name)}
        )

    try:
        root = private_directory(path)
        c = load_config(root)
        snapshot = status_snapshot(root)
        checks.append({"name": "profile", "ok": True})
    except Exception as exc:
        failure("profile", exc)
        return {"ok": False, "checks": checks, "submittedTransaction": False}
    domain = scope(c["network"], c["netuid"])
    checks.append(
        {
            "name": "journal",
            "ok": snapshot["state"] != "blocked",
            "state": snapshot["state"],
            "message": "Uncertain submissions require reconcile; never delete the journal."
            if snapshot["state"] == "blocked"
            else "No uncertain submission. Normal reveal waiting does not require a retry.",
        }
    )
    try:
        read_private(root / "authorization.json")
        consent = check_authorization(
            root / "authorization.json", domain, c["validatorHotkey"], c["versionKey"]
        )
        used = sum(
            r["state"] != "not_submitted"
            and r["record"].get("authorizationId") == consent["id"]
            for r in journal_rows(root)
        )
        if used >= consent["maxSubmissions"]:
            raise ValueError("mainnet authorization submission budget exhausted")
        checks.append(
            {
                "name": "authorization",
                "ok": True,
                "expiresAt": consent["expiresAt"],
                "remainingSubmissions": consent["maxSubmissions"] - used,
                "required": False,
            }
        )
    except Exception as exc:
        failure("authorization", exc, required=False)
    # Reading a public wallet address never unlocks or signs with the private key.
    try:
        if not c.get("wallet"):
            raise ValueError("wallet_not_configured")
        import bittensor as bt

        wallet = bt.Wallet(
            c["wallet"],
            c.get("walletHotkey", "default"),
            **({"path": c["walletPath"]} if c.get("walletPath") else {}),
        )
        if wallet.hotkeypub.ss58_address != c["validatorHotkey"]:
            raise ValueError("wallet does not match intended validator")
        checks.append(
            {
                "name": "wallet",
                "ok": True,
                "required": False,
                "message": "Public hotkey matches. Private key unlock was not attempted.",
            }
        )
    except Exception as exc:
        failure("wallet", exc, required=False)
    if not offline:
        epoch = None
        try:
            import bittensor as bt

            async with bt.Client(
                network=read_network(domain),
                fallback_endpoints=[],
                archive_endpoints=[],
                retry_forever=False,
            ) as client:
                if await client._substrate.block_hash(0) != domain["genesis"]:
                    raise ValueError("wrong_chain")
                head_hash = await client._substrate.raw.get_chain_finalised_head()
                head = await client._substrate.raw.get_block_number(head_hash)
                params = await client.subnets.subnet_hyperparameters(
                    domain["netuid"], block=head
                )
                period = params["tempo"] + 1
                if (
                    not 2 <= period <= 1000
                    or c["versionKey"] < params["weights_version"]
                ):
                    raise ValueError("unsupported_chain_policy")
                epoch = head // period - 1
                checks.append(
                    {
                        "name": "chain",
                        "ok": True,
                        "finalizedBlock": head,
                        "latestClosedEpoch": epoch,
                    }
                )
                graph = await client.subnets.metagraph(
                    domain["netuid"], block=head, commitments=False
                )
                member = next(
                    (n for n in graph.neurons if n.hotkey == c["validatorHotkey"]), None
                )
                if not member or member.validator_permit is not True:
                    raise ValueError("validator has no permit")
                checks.append({"name": "permit", "ok": True, "uid": member.uid})
        except Exception as exc:
            failure("permit" if epoch is not None else "chain", exc)
        if epoch is not None and epoch >= 0:
            try:
                raw = await asyncio.to_thread(fetch, c["feed"], epoch)
                verify(raw, c["publicKey"], domain, epoch)
                checks.append({"name": "feed", "ok": True, "epoch": epoch})
            except FeedUnavailable:
                checks.append(
                    {
                        "name": "feed",
                        "ok": False,
                        "state": "waiting",
                        "code": "newest_epoch_not_published",
                        "message": "The publisher has not published the latest closed epoch. No zero-work epoch will be substituted.",
                    }
                )
            except Exception as exc:
                failure("feed", exc)
    return {
        "ok": all(item["ok"] for item in checks if item.get("required", True)),
        "signingPrerequisitesOk": all(item["ok"] for item in checks)
        if not offline
        else None,
        "networkChecksPerformed": not offline,
        "checks": checks,
        "submittedTransaction": False,
    }
