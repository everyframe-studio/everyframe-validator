"""Pinned-SDK adapter for durable transaction identity and read-only recovery.

Keep policy planning intact, but persist the exact signed transaction hash before
the first network broadcast. Signed transaction bytes are never written to disk.
"""

import hashlib
import json
import re

HASH = re.compile(r"0x[0-9a-f]{64}\Z")
SCAN_LIMIT = 20
MORTAL_PERIOD = 64


def definitely_not_included(row):
    """Only a complete, finalized absence proof releases an attempted slot."""
    if row['state'] == 'not_submitted':
        return True
    if row['state'] != 'expired':
        return False
    result = row.get('result') or {}
    broadcast = result.get('broadcast') or {}
    proof = result.get('expiryEvidence') or {}
    mortality = broadcast.get('mortality') or {}
    first, last = proof.get('scannedFrom'), proof.get('scannedThrough')
    end, head = mortality.get('expiresAtOrBefore'), proof.get('finalizedBlock')
    before, after = mortality.get('beforeSigning'), mortality.get('afterSigning')
    block = row.get('record', {}).get('block')
    return (proof.get('reason') == 'mortal_transaction_expired_without_inclusion'
            and isinstance(broadcast.get('hash'), str) and HASH.fullmatch(broadcast['hash']) is not None
            and proof.get('transactionHash') == broadcast['hash']
            and mortality.get('period') == MORTAL_PERIOD
            and all(type(v) is int for v in (first, last, end, head, before, after, block))
            and first == broadcast.get('scanFrom') and first >= 0
            and first >= block - 1 and last == broadcast.get('scanThrough')
            and first <= before <= after <= head
            and end == after + MORTAL_PERIOD
            and proof.get('expiresAtOrBefore') == end
            and last >= end - 1 and head >= end
            and isinstance(proof.get('finalizedBlockHash'), str)
            and HASH.fullmatch(proof['finalizedBlockHash']) is not None)


async def submit_and_confirm(client, signed, signer, db, run_id, record, *,
                             timeout_seconds=40, poll_seconds=2):
    """Broadcast once, then confirm by exact hash using finalized reads.

    Subscription disconnects cannot lose the identity of the transaction. A
    missing receipt stays uncertain; it never triggers another broadcast here.
    """
    import asyncio
    from bittensor.result import ExtrinsicResult
    outcome = await client._substrate.submit_signed(
        signed, signer, wait_for_inclusion=False, wait_for_finalization=False)
    if not outcome.success or (outcome.block_hash and outcome.extrinsic_id):
        return outcome
    stored = json.loads(db.execute('SELECT result FROM weight_runs WHERE id=?', (run_id,)).fetchone()[0])
    stored['broadcastAccepted'] = True
    db.execute('UPDATE weight_runs SET result=? WHERE id=?', (json.dumps(stored), run_id))
    db.commit()
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return None
        async def check():
            head_hash = await client._substrate.raw.get_chain_finalised_head()
            head = await client._substrate.raw.get_block_number(head_hash)
            return await recover(client, db, run_id, record, stored, head)
        try:
            confirmation = await asyncio.wait_for(check(), timeout=remaining)
        except asyncio.TimeoutError:
            return None
        if confirmation['state'] in ('pending_reveal', 'finalized', 'chain_failed'):
            saved = json.loads(db.execute('SELECT result FROM weight_runs WHERE id=?', (run_id,)).fetchone()[0])
            return ExtrinsicResult(success=saved['success'], block_hash=saved['block_hash'],
                                   extrinsic_id=saved['extrinsic_id'])
        await asyncio.sleep(min(poll_seconds, max(0, deadline - asyncio.get_running_loop().time())))


async def prepare(client, intent, signer, journal, run_id, start_block, validate_plan=None):
    plan = await client.plan(intent, signer)
    if not plan.ok:
        raise ValueError("submission_policy_rejected")
    extra = validate_plan(plan) if validate_plan else {}
    nonce = await client._substrate.account_next_index(signer.ss58_address)
    before_hash = await client._substrate.raw.get_chain_finalised_head()
    before = await client._substrate.raw.get_block_number(before_hash)
    raw, tx_hash = await client._substrate.sign_extrinsic(
        plan.call, signer, nonce=nonce, period=MORTAL_PERIOD
    )
    after_hash = await client._substrate.raw.get_chain_finalised_head()
    after = await client._substrate.raw.get_block_number(after_hash)
    if type(before) is not int or type(after) is not int or not start_block <= before <= after:
        raise ValueError("invalid_signing_head")
    if (
        not HASH.fullmatch(tx_hash)
        or tx_hash != "0x" + hashlib.blake2b(raw, digest_size=32).hexdigest()
    ):
        raise ValueError("invalid_transaction_hash")
    broadcast = {
        **extra,
        "hash": tx_hash,
        "scanFrom": max(0, start_block - 1),
        "scanThrough": max(0, start_block - 1) - 1,
        # The pinned SDK anchors its mortal era at a finalized head read
        # inside sign_extrinsic. These enclosing reads give a conservative
        # expiry bound without persisting signed transaction bytes.
        "mortality": {"period": MORTAL_PERIOD, "beforeSigning": before,
                      "afterSigning": after, "expiresAtOrBefore": after + MORTAL_PERIOD},
        "nonce": nonce,
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
    """Resolve exact inclusion, or proven expiry after complete absence scanning.

    Legacy entries without a verified mortality bound remain blocked. Missing
    archive data never proves absence. This function cannot sign or submit;
    expired attempts remain in the journal and are never replayed here.
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
    mortality = broadcast.get("mortality")
    expiry = None
    if mortality is not None:
        if not isinstance(mortality, dict):
            raise ValueError("invalid_transaction_mortality")
        before, after = mortality.get("beforeSigning"), mortality.get("afterSigning")
        expiry = mortality.get("expiresAtOrBefore")
        if (mortality.get("period") != MORTAL_PERIOD
                or any(type(x) is not int for x in (before, after, expiry))
                or not first <= before <= after <= finalized_block
                or expiry != after + MORTAL_PERIOD):
            raise ValueError("invalid_transaction_mortality")
    end = min(finalized_block, cursor + SCAN_LIMIT,
              expiry - 1 if expiry is not None else finalized_block)
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
    if expiry is not None and finalized_block >= expiry and broadcast["scanThrough"] >= expiry - 1:
        head_hash = await client._substrate.block_hash(finalized_block)
        if not isinstance(head_hash, str) or not HASH.fullmatch(head_hash):
            raise ValueError("recovery_block_unavailable")
        evidence = {"reason": "mortal_transaction_expired_without_inclusion",
                    "finalizedBlock": finalized_block, "finalizedBlockHash": head_hash,
                    "scannedFrom": first, "scannedThrough": broadcast["scanThrough"],
                    "expiresAtOrBefore": expiry, "transactionHash": tx_hash}
        result["expiryEvidence"] = evidence
        db.execute("UPDATE weight_runs SET state='expired',result=? WHERE id=? AND state IN ('submitting','unknown')",
                   (json.dumps(result), run_id))
        db.commit()
        return {**report, "state": "expired", **evidence,
                "next": "Continue the current epoch schedule; the expired attempt was not replayed."}
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
