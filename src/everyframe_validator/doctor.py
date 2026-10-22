"""Read-only readiness checks, also usable while the signing loop owns its lock."""

from .core.chain_scope import scope, check_authorization, read_network, rpc_options
from .diagnostics import diagnostic
from .wallet import checked_wallet, wallet_paths
from .feed import fetch, FeedUnavailable
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
        return {"ok": False, "readOnlyOk": False, "signingPrerequisitesOk": None,
                "networkChecksPerformed": False, "checks": checks, "submittedTransaction": False}
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
        wallet, identity = checked_wallet(c, with_identity=True)
        checks.append(
            {
                "name": "wallet",
                "ok": True,
                "required": True,
                "walletPaths": wallet_paths(wallet),
                "publicIdentitySource": identity["publicIdentitySource"],
                "publicIdentityVerified": identity["publicIdentityVerified"],
                "message": ("Hotkey exists and public metadata matches. Private key unlock and signing were not attempted."
                            if identity["publicIdentityVerified"] else
                            "Encrypted hotkey exists. Public identity is not verified until explicit private-key unlock; doctor did not decrypt or sign."),
            }
        )
    except Exception as exc:
        failure("wallet", exc, required=bool(c.get("wallet")))
    if not offline:
        epoch = None
        try:
            import bittensor as bt

            async with bt.Client(
                network=read_network(domain),
                **rpc_options(domain),
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
                from . import assignment
                from .scheduled import schedule
                try:
                    view = {'block':head,'params':params}
                    s = await schedule(client,domain,head)
                    raw = await asyncio.to_thread(fetch,assignment.feed_url(c['feed']),s['SubnetEpochIndex'])
                    selected = assignment.authenticate(raw,c['publicKey'],domain,view,s)
                    checks.append({'name':'canonicalAssignment','ok':True,'chainEpoch':s['SubnetEpochIndex'],
                                   'accountingEpoch':selected['accountingEpoch'],'mode':selected['mode']})
                    if selected['mode']=='signed_epoch':
                        raw = await asyncio.to_thread(fetch,c['feed'],selected['accountingEpoch'])
                        await assignment.verify_report(client,raw,c['publicKey'],domain,view,selected)
                    checks.append({'name':'feed','ok':True,'epoch':selected['accountingEpoch']})
                except FeedUnavailable:
                    checks.append({'name':'canonicalAssignment','ok':False,'state':'waiting',
                                   'code':'canonical_assignment_or_report_not_published',
                                   'message':'The canonical signed assignment or its exact report is unavailable. No older report or HTTP-error burn will be substituted.'})
                except Exception as exc:
                    failure('canonicalAssignment',exc)
        except Exception as exc:
            failure("permit" if epoch is not None else "chain", exc)
    all_checks_ok = all(item["ok"] for item in checks)
    identity_deferred = any(item.get("publicIdentityVerified") is False for item in checks)
    return {
        "ok": all(item["ok"] for item in checks if item.get("required", True)),
        "readOnlyOk": all(item["ok"] for item in checks
                          if item.get("required", True) and item["name"] != "wallet"),
        "signingPrerequisitesOk": (False if not all_checks_ok else None if identity_deferred else True)
        if not offline else None,
        "networkChecksPerformed": not offline,
        "checks": checks,
        "submittedTransaction": False,
    }
