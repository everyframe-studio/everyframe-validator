"""External validator loop. Fetch → authenticate → score → optional local signing."""
import asyncio
from contextlib import redirect_stdout
import io
import json
import time
from types import SimpleNamespace

from .core import validator, reconcile_weights
from .core.chain_scope import scope, read_network, check_authorization
from .core.reward_policy import seal_epoch, digest
from .feed import fetch, verify, FeedUnavailable
from .state import locked, load_config, journal_rows, atomic_json, read_private


async def chain_target(config):
    import bittensor as bt
    domain = scope(config["network"], config["netuid"])
    async with bt.Client(network=read_network(domain), fallback_endpoints=[], archive_endpoints=[], retry_forever=False) as client:
        if await client._substrate.block_hash(0) != domain["genesis"]:
            raise ValueError("wrong_chain")
        head_hash = await client._substrate.raw.get_chain_finalised_head()
        head = await client._substrate.raw.get_block_number(head_hash)
        params = await client.subnets.subnet_hyperparameters(domain["netuid"], block=head)
        period = params["tempo"] + 1
        if not 2 <= period <= 1000 or config["versionKey"] < params["weights_version"]:
            raise ValueError("unsupported_chain_policy")
        return head // period - 1, head // period


async def captured(call, args):
    output = io.StringIO()
    with redirect_stdout(output):
        await call(args)
    return [json.loads(line) for line in output.getvalue().splitlines() if line.startswith("{")]


async def reconcile(root, config):
    args = SimpleNamespace(network=config["network"], netuid=config["netuid"],
                           journal=str(root / "journal.db"), validator_hotkey=config["validatorHotkey"])
    return await captured(reconcile_weights.main, args)


async def tick(root, submit=False, only_reconcile=False):
    with locked(root) as root:
        c = load_config(root)
        domain = scope(c["network"], c["netuid"])
        rows = journal_rows(root)
        if any(r["state"] in ("pending_reveal", "finalized") for r in rows):
            reports = await reconcile(root, c)
            if only_reconcile:
                return {"state": "reconciled", "reports": reports, "submittedTransaction": False}
            rows = journal_rows(root)
        if only_reconcile:
            return {"state": "no_confirmed_submission_to_reconcile", "submittedTransaction": False}
        unresolved = [r for r in rows if r["state"] in ("submitting", "unknown", "pending_reveal", "finalized")]
        if unresolved:
            return {"state": "blocked", "reason": "unresolved_submission", "runs": [{"id": r["id"], "state": r["state"]} for r in unresolved]}
        epoch, open_epoch = await chain_target(c)
        if epoch < 0:
            return {"state": "waiting", "reason": "no_closed_epoch"}
        if any(r["state"] != "not_submitted" and (r["record"]["epoch"] >= epoch or r["record"]["openEpoch"] >= open_epoch) for r in rows):
            return {"state": "already_submitted", "epoch": epoch}
        if submit:
            read_private(root / "authorization.json")
            check_authorization(root / "authorization.json", domain, c["validatorHotkey"], c["versionKey"])
        try:
            raw = await asyncio.to_thread(fetch, c["feed"], epoch)
        except FeedUnavailable:
            return {"state": "waiting", "reason": "newest_epoch_not_published", "epoch": epoch}
        payload = verify(raw, c["publicKey"], domain, epoch)
        # Store the WHOLE signed payload hash immutably, including identity bindings.
        # The artifact DB hash alone would not detect a conflicting hotkey map.
        cache = root / "accepted"
        cache.mkdir(mode=0o700, exist_ok=True)
        atomic_json(cache / (str(epoch) + ".json"), {"payloadHash": digest(payload)}, immutable=True)
        seal_epoch(root / "epochs.db", payload["artifact"])
        atomic_json(root / "bindings.json", payload["bindings"])
        args = SimpleNamespace(network=c["network"], netuid=c["netuid"], submit=submit,
            mainnet_authorization=str(root / "authorization.json"), epochs=str(root / "epochs.db"),
            bindings=str(root / "bindings.json"), validator_hotkey=c["validatorHotkey"], version_key=c["versionKey"],
            wallet=c.get("wallet"), wallet_hotkey=c.get("walletHotkey", "default"), wallet_path=c.get("walletPath"),
            wallet_password_file=c.get("walletPasswordFile"), journal=str(root / "journal.db"), test_job_id=[])
        records = await captured(validator.main, args)
        record = next((r for r in records if "plan" in r), None)
        if record is None:
            raise ValueError("missing_weight_plan")
        result = {"state": "dry_run", "epoch": epoch, "netuid": c["netuid"], "payloadHash": digest(payload),
                  "weights": record["plan"]["quantized"], "allBurn": record["plan"]["allBurn"], "submittedTransaction": False}
        if submit:
            outcome = records[-1]
            result.update(state=outcome.get("state", "unknown"), submittedTransaction=outcome.get("state") in ("pending_reveal", "finalized"))
        return result


async def run(root, submit=False, once=False, interval=60):
    while True:
        try:
            result = await asyncio.wait_for(tick(root, submit=submit), timeout=300)
            failed = result["state"] == "blocked"
        except Exception as exc:
            failed = True
            result = {"state": "failed", "errorType": type(exc).__name__,
                      "message": "Check feed trust, chain access, wallet configuration and journal. Never clear uncertain submissions to retry."}
        print(json.dumps(result), flush=True)
        # Status is diagnostic, never a source of submission authorization.
        with locked(root) as directory:
            atomic_json(directory / "status.json", {**result, "atMs": int(time.time()*1000)})
        if once:
            return 2 if failed else 0
        await asyncio.sleep(interval)
