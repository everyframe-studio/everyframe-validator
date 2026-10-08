"""Pinned-SDK adapter for durable transaction identity and read-only recovery.

Keep policy planning intact, but persist the exact signed transaction hash before
the first network broadcast. Signed transaction bytes are never written to disk.
"""

import hashlib
import json
import re

HASH = re.compile(r"0x[0-9a-f]{64}\Z")
SCAN_LIMIT = 20


async def prepare(client, intent, signer, journal, run_id, start_block):
    plan = await client.plan(intent, signer)
    if not plan.ok:
        raise ValueError("submission_policy_rejected")
    nonce = await client._substrate.account_next_index(signer.ss58_address)
    raw, tx_hash = await client._substrate.sign_extrinsic(
        plan.call, signer, nonce=nonce, period=64
    )
    if (
        not HASH.fullmatch(tx_hash)
        or tx_hash != "0x" + hashlib.blake2b(raw, digest_size=32).hexdigest()
    ):
        raise ValueError("invalid_transaction_hash")
    broadcast = {
        "hash": tx_hash,
        "scanFrom": max(0, start_block - 1),
        "scanThrough": max(0, start_block - 1) - 1,
    }
    changed = journal.execute(
        "UPDATE weight_runs SET result=? WHERE id=? AND state='submitting'",
        (json.dumps({"broadcast": broadcast}), run_id),
    )
    if changed.rowcount != 1:
        raise ValueError("submission_reservation_missing")
    journal.commit()  # synchronous=FULL is set by the caller, before broadcast.
    from bittensor._transport.contract import SignedExtrinsic

    return SignedExtrinsic(raw, tx_hash), broadcast


async def recover(client, db, run_id, record, result, finalized_block):
    """Only finalized inclusion of the EXACT recorded hash resolves uncertainty.

    No match (including after expiry), missing hashes, or missing archive data
    never authorizes a retry. This function cannot sign or submit transactions.
    """
    broadcast = result.get("broadcast", {})
    tx_hash = broadcast.get("hash")
    report = {"runId": run_id, "state": "blocked", "submittedTransaction": False}
    if not isinstance(tx_hash, str) or not HASH.fullmatch(tx_hash):
        return {
            **report,
            "reason": "transaction_hash_unavailable",
            "next": "Preserve the original journal for manual chain investigation. Do not resubmit or clear state.",
        }
    first, cursor = broadcast.get("scanFrom"), broadcast.get("scanThrough")
    if (
        type(first) is not int
        or type(cursor) is not int
        or first < 0
        or cursor < first - 1
        or first < record["block"] - 1
        or cursor > finalized_block
    ):
        raise ValueError("invalid_recovery_cursor")
    end = min(finalized_block, cursor + SCAN_LIMIT)
    for block in range(cursor + 1, end + 1):
        block_hash = await client._substrate.block_hash(block)
        if not isinstance(block_hash, str) or not HASH.fullmatch(block_hash):
            raise ValueError("recovery_block_unavailable")
        found = await client._substrate.find_extrinsic(tx_hash, block_hash)
        if found is not None:
            extrinsic_id = found.extrinsic_id
            if (
                found.block_hash != block_hash
                or not isinstance(extrinsic_id, str)
                or not re.fullmatch(r"\d+-\d+", extrinsic_id)
                or int(extrinsic_id.split("-")[0]) != block
                or type(found.success) is not bool
            ):
                raise ValueError("invalid_recovery_evidence")
            state = (
                ("pending_reveal" if record["commitReveal"] else "finalized")
                if found.success
                else "chain_failed"
            )
            result = {
                "broadcast": broadcast,
                "success": found.success,
                "block_hash": block_hash,
                "extrinsic_id": extrinsic_id,
                "recoveredFromFinalizedChain": True,
            }
            db.execute(
                "UPDATE weight_runs SET state=?,result=? WHERE id=? AND state IN ('submitting','unknown')",
                (state, json.dumps(result), run_id),
            )
            db.commit()
            return {
                **report,
                "state": state,
                "transactionHash": tx_hash,
                "includedBlock": block,
                "reason": "exact_transaction_finalized",
                "next": "Continue reconciliation; no transaction was resubmitted.",
            }
        broadcast["scanThrough"] = block
    db.execute(
        "UPDATE weight_runs SET result=? WHERE id=? AND state IN ('submitting','unknown')",
        (json.dumps(result), run_id),
    )
    db.commit()
    return {
        **report,
        "reason": "exact_transaction_not_found_yet",
        "transactionHash": tx_hash,
        "scannedThrough": broadcast["scanThrough"],
        "finalizedBlock": finalized_block,
        "next": "Run reconcile again to continue the bounded finalized-chain search. Absence is not permission to retry.",
    }
