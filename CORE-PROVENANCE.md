# Reward engine provenance

The `core` package was adapted on 2026-09-21 from this workspace's EveryFrame
subnet `verifier/{chain_scope,reward_policy,validator,reconcile_weights}.py`.
It is bundled, not imported from the sibling repository at runtime. It contains
EveryFrame's existing policy, not copied SayGM implementation code.

Preserved behavior: exact cap/burn arithmetic and minimum-positive rounding,
SDK quantization, ownership/permit checks, chain provenance, pre-sign revalidation,
durable submission reservation, no uncertain-broadcast retry, and reveal checks.

Packaging changes:

- Canonical chain-epoch assignments replace local report queues for
  scoring selection. Each validator still computes exact weights locally; the
  signed assignment binds only accounting inputs and finalized chain timing.
- Owner-only accounting is bundled under `everyframe_validator.owner`, separately
  from the validator loop. This ports the tested private-provenance finalizer and
  recovery ledger from the subnet verifier using package-relative imports.
  Independent validation never invokes approvals, finalization or recovery
  settlement. Public recovery totals use the existing signed score fields;
  private evidence and financial approval stay with the publisher.
- Relative imports and bundled immutable network profiles.
- No owner-runner or SSH snapshot execution in the independent validator loop.
- Mainnet defaults to the official Finney archive endpoint; operators can explicitly select
  another RPC. Genesis verification remains required, and missing historical
  state fails closed rather than silently switching networks or resetting state.
- v0.5.1 adds EveryFrame's own Etao miner-bucket reconstruction and public
  evidence checks. The pure `core/emission_policy.py` is mirrored byte-for-byte
  into the subnet verifier. It reads the chain's vector and rates, not a copied
  SayGM implementation or a fixed replacement percentage. Signed historical
  policies retain their original interpretation. The writer gate remains opt-in.
- Local operator consent allows up to 365 days/1,000,000 submissions rather than
  the owner's 24-hour/3-submission pilot restriction. It applies on both networks.
  Consent is not issued or approved by the subnet owner.
- Cancelled submissions are journaled as uncertain (`BaseException` handling).
- Confirmed but not yet read-back non-timelock submissions also block new signing.
- Reject accounting that claims finalization beyond the current finalized chain head.
- Shared read-only wallet preflight validates parent paths, file presence and
  public identity without unlocking or rewriting keys. Signing separately checks
  the unlocked private signer's identity before reserving a submission.
- The pinned Bittensor 11.1.0 submission adapter uses policy-checked intent planning,
  records the signed transaction hash before a single transport submission, and
  can recover uncertain broadcasts using exact-hash finalized inclusion. Missing
  evidence never permits automatic resubmission. Reconciliation still independently
  verifies finalized weights and reveal events after a recovered successful commit.

Only the authenticated aggregate path is exposed by the external CLI. Internal
legacy synthetic/count helpers remain for source compatibility, but the runner
never supplies test-job IDs or a private coordinator database. No external CLI
switch can enable synthetic work rewards.

Mainnet remains an explicit operator action. Creating, installing, building or
testing this package does not authorize transactions or production deployment.
