# Signed finalized epoch feed v1

Each `{epoch}.json` is one JSON object with exactly `payload` and `signature`.
The signature is standard base64 Ed25519 over UTF-8 encoding of
`json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)`.
This format uses Python JSON escaping (`ensure_ascii=True`), not arbitrary JSON
stringification. The pinned public key is base64 DER SubjectPublicKeyInfo.
Duplicate keys, floats, non-finite values, extra wire fields and oversized bodies
are rejected. Maximum document size is 2,000,000 bytes and maximum scored miners 4096.

`payload` has exactly:

- `schema`: `everyframe.validator.epoch.v1`.
- `artifact`: existing version-2 `epoch-value-cap-burn-v2` metadata, economics,
  and aggregate scoring window. No per-job `proofs` or `excluded` lists.
- `bindings`: exactly the scored miner IDs mapped to distinct SS58 hotkeys.
- `sourceHash`: SHA-256 of the original private sealed artifact's canonical JSON.
  This is a commitment, not proof that unpublished private evidence is correct.

The artifact binds network, netuid, genesis, epoch, period, block range and boundary
hashes. Testnet is strictly test/SN566; mainnet strictly finney/SN117. Mainnet also
includes owner-cut evidence, miner share, recycle/burn disposition,
and `accountingWindow: fixed-tempo-plus-one-v1`. These fixed accounting windows are
not represented as subnet-offset Yuma distribution epochs.

New mainnet accounting uses `minerSharePolicy: fixed-41-percent-v1` and
`minerShare: "0.41"`. The pool is total epoch alpha emissions × 0.41 × alpha/USD.
Owner-cut evidence remains recorded but does not adjust this fixed accounting
share. This does not change chain emission parameters or add a miner premium.
Legacy signed reports without the policy field retain their original
chain-derived share; they are neither rewritten nor replayed. Unknown policies
or inconsistent shares are rejected. Upgrade feed consumers and the signing
publisher before enabling the new finalizer: older validators reject the new
field.

Window fields: `version`, `policy`, `start`, `end`, `finalizedAt`, `scores`.
Scores contain `minerId`, integer `earningsMicrousd`, integer `surchargeMicrousd`,
and integer `jobs`. Price provenance permits only `source`, `observedAt`, `taoUsd`,
and `candleHash`. The publisher uses explicit allowlists at each level.

Scores represent eligible reviewed work recognized in this report; completion may
predate the window when review was delayed. The owner-side private sealed proof
journal prevents repeated recognition of a job or attempt. Pending reviews are
deferred, not counted as approved, and do not block unrelated finalized work.
Historical reports remain immutable; late work is recognized in a future report,
not by asking validators to submit stale reports. Private proof identifiers and
review status are not added to the public wire format.

Accounting is authenticated, not independently reconstructed. Validators recompute
the value-based cap/burn allocation locally with exact rational arithmetic, then
use the pinned Bittensor SDK's normalization. Current chain constraints may force a
defer; the validator never invents work or changes chain settings to fit the vector.

Accounting epoch numbers come from finalized chain head divided by `tempo+1`, minus one.
Scheduling slots instead use finalized `SubnetEpochIndex` and `LastEpochBlock`.
The v0.5.0 `chain-epoch-v3` scheduler uses an additional signed assignment at
the sibling `allocations/{chainEpoch}.json` endpoint, never a local backlog.
The accounting report format above remains compatible with older readers.

Assignment payload fields are exactly `schema`, `policy`, `network`, `netuid`,
`genesis`, `chainEpoch`, `chainEpochStart`, `chainEpochStartHash`,
`previousChainEpochStart`, `cutoffBlock`, `accountingEpoch`, `blocksPerEpoch`,
`mode` and `payloadHash`. Schema is `everyframe.validator.assignment.v1`, policy
is `epoch-start-accounting-v1`; envelope signing is identical to report signing.
Epoch starts and the previous start are read from finalized historical storage.
Cutoff is the current chain start; target is `cutoffBlock // blocksPerEpoch - 1`.
`signed_epoch` binds the full report payload hash. If the previous chain-start
cutoff targeted the same report, mode is `scheduled_reuse_burn` with a null report
hash: every validator computes the same verified owner-only burn instead of
reallocating that report. No validator-specific history participates in selection.

404 and transport failure wait, never stand in for a signed zero-work report.
Assignments and accepted report hashes are immutable locally. A report already
submitted under a prior policy cannot be replayed at cutover. Existing journals
and pending commitments remain authoritative for duplicate/uncertainty guards.
Finalized reveal and mechanism-step evidence are stored separately from transaction
finality and do not prove alpha settlement.
The complete payload hash (including bindings) is stored immutably;
even another valid signature cannot revise an already accepted epoch locally.
No discovery listing, mutable latest pointer, S3 login, or owner SSH is needed.

## Missed-reward carry-forward

New public score totals can include a reviewed correction to prior earnings.
Original work is not recognized twice. Private source report hashes, recovery
assessments and claim reservations remain in the owner ledger, committed by
`sourceHash`, and are not leaked as job/payment evidence in the public projection.
External validators still trust the operator's aggregate accounting, not an
independent proof of the shortfall or a USD payout guarantee.

A correction is original eligible earnings minus rewards already received under
a finalized, all-validator review. An owner missed submission is insufficient.
Pending commitments and uncertain observations defer review. Corrections use
the future epoch's real remaining pool after current work; no old emission pool
is added, no historical signed report is changed, and insufficient capacity stays
outstanding. Sealing reserves a claim as in flight, not paid. A reviewed failed or
partial outcome reopens only its unpaid remainder; unreconciled claims never retry.
