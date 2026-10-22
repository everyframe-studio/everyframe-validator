# Sparse accounting recovery

Historical v0.4.0/v0.4.1 behavior below. The unreleased canonical-assignment
patch supersedes local backlog scanning for weight selection: see
[PROTOCOL.md](PROTOCOL.md). Preserve these old cursor files for audit; do not
replay their backlog after upgrading. Reviewed reward shortfalls belong in new
owner accounting and are shared by all validators.

The publisher finalizes the newest closed accounting window. An RPC outage can
leave a hole in its epoch numbers; recognition of eligible jobs carries forward
through its private sealed-work ledger. Requiring every epoch number in sequence
therefore blocks newer approved work after an otherwise recovered outage.

The validator now checks for later signed reports without consuming missing
reports. Each available report must pass the existing signature, scope, chain
boundary, immutable-cache and accounting checks. The submission journal still
enforces at most one submission per report and one allocation per chain epoch.
Late reports remain eligible; already-submitted reports are not replayed.

Scans check at most 32 unsubmitted epochs and have a 30-second overall deadline.
An incomplete scan waits, never fabricates a zero-work report. The independent
`accounting-scan.json` cursor rotates long scans and resets after finding work;
it is not a payment or consumption ledger. Signed zero-work reports are verified
and cached, but do not delay positive work by consuming separate chain slots.

## Deployment

This fix changes `scheduled.py` only. It works with existing legacy signed
mainnet accounting and does not require the separate fixed-41% feed policy.
Preserve `reward-schedule.json`, all journals, accepted reports, authorizations,
and pending commitments. Do not manually advance the accounting cursor or
replay historical weights. Stop the single owner loop gracefully before changing
its runtime, and retain its zero-fee and per-chain-epoch constraints.

## Verification

Tests cover sparse feeds, late gap arrival, already-submitted reports,
authenticated empty reports, signature/transport failures, long rotating scans,
dry-run cursor immutability, and existing signing/reveal guards. A live read-only
check on 2026-10-21 verified epoch 25840 for $1.170662 after missing epoch 25836
and verified empty reports 25837–25839, without signing or broadcasting.
Application and actual alpha settlement must still be observed separately.
