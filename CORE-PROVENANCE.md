# Reward engine provenance

The `core` package was adapted on 2026-09-21 from this workspace's Everyframe
subnet `verifier/{chain_scope,reward_policy,validator,reconcile_weights}.py`.
It is bundled, not imported from the sibling repository at runtime. It contains
Everyframe's existing policy, not copied SayGM implementation code.

Preserved behavior: exact cap/burn arithmetic and minimum-positive rounding,
SDK quantization, ownership/permit checks, chain provenance, pre-sign revalidation,
durable submission reservation, no uncertain-broadcast retry, and reveal checks.

Packaging changes:

- Relative imports and bundled immutable network profiles.
- No owner CLI entrypoints or owner runner/SSH snapshot code.
- Mainnet uses the pinned official archive endpoint for historical boundary reads.
- Local operator consent allows up to 365 days/1,000,000 submissions rather than
  the owner's 24-hour/3-submission pilot restriction. It applies on both networks.
  Consent is not issued or approved by the subnet owner.
- Cancelled submissions are journaled as uncertain (`BaseException` handling).
- Confirmed but not yet read-back non-timelock submissions also block new signing.
- Reject accounting that claims finalization beyond the current finalized chain head.

Only the authenticated aggregate path is exposed by the external CLI. Internal
legacy synthetic/count helpers remain for source compatibility, but the runner
never supplies test-job IDs or a private coordinator database. No external CLI
switch can enable synthetic work rewards.

Mainnet remains an explicit operator action. Creating, installing, building or
testing this package does not authorize transactions or production deployment.
