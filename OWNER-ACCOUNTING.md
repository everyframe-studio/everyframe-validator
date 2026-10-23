# Owner accounting and missed-reward recovery (v0.5.0+)

Owner-only tools are bundled in `everyframe_validator.owner` and integrated into
`everyframe-finalize-epoch`. Installing v0.5.0 does not approve recoveries, invoke
these commands, change historical balances or authorize chain signing.

## What is carried

A reviewed shortfall is original eligible, privately proven earnings minus
rewards already received according to a finalized global emission review.
Do not use an owner validator's missed submission as proof of zero payment:
other validators, older revealed weights and later pending commitments can reward
the same miner. Valuation and attribution are explicit operator-reviewed evidence,
not a claim that the software independently reconstructs per-job USD settlement.
The CLI checks the evidence's finalized chain anchor; it cannot authenticate the
truth of an operator's off-chain attribution by hashing a file.

Sealed reports and original job recognition never change. The ledger records a
unique source epoch/hash/miner recovery, preventing duplicate approval. Current
approved work is counted first, then oldest reviewed shortfalls fill the new
epoch's remaining real pool. Recovery stays in `earningsMicrousd`; surcharge and
emissions/price are not invented. Partial capacity leaves the remainder pending.
No transfers, Studio credits, provider keys, receipts or coordinator payables change.

Only mapped, enabled, currently registered non-owner recipients are eligible.
Disabled/unregistered recipients remain owed but do not block unrelated current
work. `--recovery-bindings` explicitly enables this eligibility check in finalization;
without that mapping, recoveries remain unassigned.

## States and crash protection

- Reviewed shortfall: outstanding and available for a future allocation.
- Sealed recovery claim: atomically reserved as **in flight**, not paid.
- Reviewed received amount: closes only that part of the outstanding shortfall.
- Reviewed zero/partial recovery outcome: reopens only its unpaid remainder.
- Pending/uncertain outcome: stays in flight; never automatically reissued.

Assessments, allocations, outcomes and immutable epoch seals share the epoch DB.
Sealing reserves claims under `BEGIN IMMEDIATE`, with the epoch insert in the
same transaction. Idempotent retries, concurrent snapshots, rollback, stale
claims and missing/corrupt ledger rows cannot assign the same outstanding amount
twice. Source and destination report hashes are rechecked. An adjustment cannot
be recursively treated as a new original job; a failed attempt reconciles back
to its original recovery ID instead.

`assignedMicrousd` is cumulative allocation attempts, **not money received**.
`inFlightMicrousd` is unresolved allocation value. `reviewedReceivedMicrousd` is
the amount confirmed by explicit operator review. `outstandingMicrousd` remains
the original shortfall minus that reviewed received amount; `unassignedMicrousd`
subtracts in-flight reservations too. Emission caps and consensus still apply;
already distributed/burned historical emissions cannot be recreated.

## Commands

For Etao-aware accounting, the v0.5.1 reader/publisher update adds
`--chain-emission-policy` to finalization. Enable it only after coordinating all
validator upgrades; v0.5.0 rejects the new signed evidence. Until that explicit
cutover, installing compatible readers does not change the report policy.
Recoveries then consume the chain-derived miner bucket, not an assumed 41% pool.
Historical shortfalls still require global review; this update does not approve
compensation automatically. See [PROTOCOL.md](PROTOCOL.md).

Use the existing trusted publisher environment with the updated validator
package available for strict JSON decoding. Preserve the DB and journals.

```bash
everyframe-recover-rewards status --network finney \
  --epochs /var/lib/everyframe-mainnet-publisher/epochs.db

everyframe-recover-rewards audit-owner --network finney \
  --epochs /var/lib/everyframe-mainnet-publisher/epochs.db \
  --journal /absolute/path/to/existing-owner-journal.db

everyframe-recover-rewards approve --network finney \
  --epochs /var/lib/everyframe-mainnet-publisher/epochs.db \
  --assessment /absolute/private/reviewed-shortfall.json \
  --confirm-global-reward-review

everyframe-recover-rewards settle --network finney \
  --epochs /var/lib/everyframe-mainnet-publisher/epochs.db \
  --assessment /absolute/private/reviewed-recovery-outcome.json \
  --confirm-global-reward-review
```

Approval JSON fields: `version: 1`, `policy:
global-finalized-emission-review-v1`, network/netuid/genesis, `sourceEpoch`,
`sourceHash` of the complete private source report, `minerId`, integer
`observedRewardMicrousd`, and `evidence`. Expected earnings are derived from
the source's original eligible proofs, never supplied as an arbitrary amount.

Outcome JSON instead has `recoveryId`, `allocationEpoch`, `allocationHash` and
integer `observedRecoveryMicrousd`, attributed to that recovery claim, not the
miner's total income including new work. It shares version, policy, domain and
evidence fields with approval JSON. Amounts cannot exceed the sealed claim.

Evidence fields are `finalizedBlock`, `finalizedBlockHash`, half-open
`observationStartBlock`/`observationEndBlock`, `evidenceHash`,
`allValidatorRewardsReviewed: true`, and `pendingCommitmentsResolved: true`.
The actual allocation/reveal interval may be later than the source accounting
window. Positive reward observation ranges cannot overlap for the same miner;
partition their attribution rather than applying the same income twice. One
identical reviewed zero-emission range can establish several missed allocations.
Never set these review flags from owner journal absence alone.

## Synchronized validator cutover

The v0.5.0 independent validator uses a signed immutable assignment at
`/mainnet/v1/validator/allocations/{chainEpoch}.json` (testnet omits `/mainnet`).
It binds the finalized epoch-start block/hash and exact accounting report hash.
The target is fixed by `LastEpochBlock // (tempo + 1) - 1`, not each validator's
private backlog. Current work plus reviewed recoveries is therefore the same
input for every validator. The publisher creates this feed with
`everyframe-publish-assignment` using its existing
accounting signing credential. No chain wallet is involved.

Deploy and verify the assignment endpoint before coordinating owner and
independent-validator activation. Missing assignments/reports wait; HTTP errors
never imply zero work. Preserve old reports, journals and pending commitments.
Do not activate owner-only new selection while other validators still replay
their private queues, or claim that a package release automatically updates them.

## Verification

```bash
python -m pytest tests/test_owner_recovery.py tests/test_owner_recognition.py \
  tests/test_owner_reward_policy.py -q
```

Tests use synthetic accounting, real pinned SDK quantization and real Ed25519
signatures. They do not generate media, load a production wallet, submit weights,
approve historical shortfalls or prove real alpha settlement.

## Assignment feed service

The optional `deploy/everyframe-assignment-publisher.service` and timer read the
existing sealed epoch DB and proven bindings, sign the exact chain-start target,
and expose immutable reports/assignments. The source accounting directory is
read-only. The service uses the existing accounting signing credential through
systemd; it never loads a chain wallet, submits weights, approves corrections or
upgrades running validators. `deploy/assignment-feed.caddy` adds only numeric
GET/HEAD routes; do not expose private directories or discovery listings.

Before enabling recovery allocations in the owner finalizer, provide the reviewed
recipient map explicitly:

```bash
everyframe-finalize-epoch --network finney --netuid 117 \
  --database /var/lib/everyframe-mainnet-publisher/accounting.db \
  --epochs /var/lib/everyframe-mainnet-publisher/epochs.db \
  --recovery-bindings /var/lib/everyframe-mainnet-publisher/resolved-bindings.json
```

Use the existing publisher lock and consistent accounting snapshot workflow;
do not run a second finalizer concurrently with the production publisher. New
claim reservations remain in flight until reviewed reconciliation, even if the
report was successfully signed and published.
