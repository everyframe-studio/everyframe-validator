# EveryFrame Validator 0.8.4

- Bundle the official mainnet treasury wallet address so validators only need to update and restart as usual.
- Remove the separate treasury acceptance step for the bundled Finney/SN117 recipient.
- Preserve existing profiles, journals, signing authorization, consent overrides and signed assignment formats.
- Keep on-chain recipient verification and coordinated future-epoch funding activation.

# EveryFrame Validator 0.8.3

- Restore Treasury wallet as the preferred name in operator commands, help and documentation.
- Prefer `accept-treasury-wallet` and `--treasury-wallet-*`; retain all contest-pool aliases.
- Preserve saved consent, signed protocols, reward calculations and activation settings.

# EveryFrame Validator 0.8.2

- Restore Contest pool as the preferred name in operator commands, help and documentation.
- Prefer `accept-contest-pool` and `--contest-pool-*`; retain the v0.8.1 treasury-wallet aliases.
- Keep saved consent, signed protocols, reward calculations and activation unchanged.

# EveryFrame Validator 0.8.1

- Use Treasury wallet in operator documentation, CLI help and diagnostics.
- Add `accept-treasury-wallet` and `--treasury-wallet-*` commands/options while retaining existing aliases.
- Preserve signed assignment formats, consent, state, reward calculations and activation status.

# EveryFrame Validator 0.8.0

- Select a prize-pool UID from all registered hotkeys under one accepted non-owner coldkey, using the signed report and finalized epoch snapshot.
- Independently verify the candidate set and selection; preserve serving weights and pin the recipient for each epoch.
- Accept the pool coldkey once; new registered pool hotkeys become eligible in subsequent snapshots.
- Preserve old assignments and existing commitments. Contest funding remains inactive until coordinated activation.

# EveryFrame Validator 0.7.0

- Add separately accounted AI Film Contest Pool funding from the unused miner allocation after serving fees, bonuses and eligible recoveries.
- Require independent verification and explicit local acceptance of the non-owner pool identity. Preserve historical burn assignments and reject policy downgrades.
- Add an opt-in host updater for official Docker Compose deployments: stable releases, immutable images, verified checksums, graceful stop, offline preflight and readiness checks. State and signing consent are preserved.
- Keep contest funding disabled until coordinated activation. The owner bonus policy is 10–50% for future reports.

# EveryFrame Validator v0.6.0

- Add deterministic owner burn selection. All validators derive the same burn
  UID from the signed accounting assignment and the registered owner hotkeys at
  its finalized epoch-start snapshot. Only unused allocation moves; serving-miner
  weights stay unchanged.
- Independently verify the candidate set, seed and selected UID. Recheck its
  ownership before signing and at reveal/application; never silently substitute
  another target if ownership changes.
- Preserve v1 assignments, existing reports, journals, authorizations and pending
  commitments. Upgrading does not activate burn rotation or change chain settings.
- Make publisher activation durable across interrupted writes and serialize
  concurrent publishers. Readers reject a silent policy downgrade after v2
  activation. Doctor reports the selected UID and verification snapshot.
- Upgrade all validators before the operator enables v2 at a future chain epoch.
  Stop the existing instance, install v0.6.0, run `doctor` with the same state and
  wallet configuration, and restart the original service. Do not reinitialize
  state. See [the rollout guide](https://github.com/everyframe-studio/everyframe-validator/blob/v0.6.0/BURN-ROTATION.md).
- Rotation can make stale copied vectors less useful. It does not hide the public
  report or guarantee a particular VTrust score.

# EveryFrame Validator v0.5.1

- Add opt-in `chain-miner-emission-v1` reports for Etao's actual subnet emission
  vector rather than assuming 41%. Sum per-block miner credits, with parent-state
  splits, rate changes and conservative U96F32/integer rounding accounted for.
- Publish signed public evidence and independently check it against finalized
  storage before computing/signing weights. Use metadata-sharing bounded reads
  and immutable full-artifact/boundary-hash caches, not repeated full SDK queries.
- Preserve existing legacy/fixed-41 signed reports, journals and commitments.
  The publisher's `--chain-emission-policy` gate stays opt-in until owner and
  independent validators are compatible. No chain settings or old reports change.
- Fix reveal/application observation for `chain-epoch-v3` records as well as v2.
- Default mainnet reads to the official Finney archive. Keep explicit RPC
  overrides. `doctor --check-emissions` probes new-policy readiness before
  activation, without signing, publishing or writing a verification cache.
- Publishing this release does not activate the new accounting policy, upgrade
  other validators, restart signing services, or approve historical compensation.
  Upgrade all readers and pass `doctor --check-emissions` on their actual RPC
  configurations before the owner explicitly enables the new writer policy.

# EveryFrame Validator v0.5.0

- Select one shared, signed accounting assignment per finalized chain epoch.
  Local backlog, start date and scan position no longer choose scoring inputs.
- Verify the assignment's epoch-start block/hash, deterministic accounting cutoff,
  exact report hash and chain provenance, while calculating weights independently.
- Missing assignments/reports always wait, even with the legacy burn flag. A
  burn requires authenticated zero-work or a signed no-new-window assignment.
- Preserve old journals, cursors and commitments. Already submitted historical
  reports are not replayed during cutover. Doctor checks canonical feed readiness.
- Owner accounting supports reviewed shortfall carry-forward in future reports,
  with original work deduplication, pool limits, in-flight reservations and
  explicit global-emission reconciliation. A seal is never payment confirmation.
- Bundle owner-only finalization, recovery audit/approval/reconciliation tools and
  a restricted assignment-feed publisher. These are not run by the independent
  validator loop. Publishing a package does not approve any financial correction.
- A dry-run canary can check the canonical plan outside the signing window;
  actual signing retains its early-window and zero-fee restrictions.
- Coordinate publisher/owner/independent deployment. v0.4.1 does not contain this
  protocol. Verify assignment-feed readiness before switching a signing process;
  upgrading a release does not automatically activate mainnet or update peers.

# EveryFrame Validator v0.4.1

- Check configured wallet paths and public identity during init and authorize,
  and before signing-mode epoch waits. Show the resolved private/public filenames
  and a verified duplicate-root suggestion instead of a generic missing-file error.
- A configured broken wallet now makes doctor fail; readOnlyOk separately reports
  non-wallet checks. Wallet-free read-only profiles remain supported.
- Read public metadata directly from existing legacy hotkeys; no separate
  hotkeypub.txt is required or generated. Diagnostics never decrypt, pass secret
  fields to keypair deserialization, or print keyfile contents. Encrypted identity
  checks may be deferred until an authorized unlock and are explicitly not
  reported verified.
- Preflight never unlocks keys or rewrites profiles/journals. Verify the actual
  private signer identity again before reserving or signing a transaction.
  No scheduling or reward changes.

# EveryFrame Validator v0.4.0

- Recover across missing accounting reports: later verified work can proceed,
  missing reports remain eligible, and already-submitted reports cannot replay.
- Verify and pass over zero-work reports before allocating a chain slot to paid
  work. Bounded incomplete scans wait; errors never become fake zero accounting.
- Independent validator upgrade: stop the previous instance, update the image
  or package, and reuse the same state, wallet mounts, RPC and signing command.
  Do not reinitialize, clear journals, or delete state volumes.

- Support explicit fixed-41% miner-pool accounting for new mainnet reports.
  Legacy signed reports retain their original share. Deploy updated feed
  consumers/publisher before the finalizer; older clients reject the new field.
  No chain emission parameter changes, historical replay, or extra reward bonus.

- Replace repeated/block and fixed-accounting-window signing with one allocation
  per actual finalized subnet epoch. Legacy CLI cadence flags map to the safe mode.
- Check chain timing, identity, cooldown and reveal-round separation before
  signing and again before broadcast; retain zero-fee policy and consent limits.
- Persist an accounting cursor so late reports are queued, not discarded or
  replaced by burn in the same reveal window. No historical reward replay.
- Record exact-commit reveal and subsequent mechanism-step observations separately
  from alpha settlement. Overwritten/ambiguous allocations require review.
- Preserve all prior submission journals and already queued commitments.

# EveryFrame Validator v0.3.0 (historical release)

## New defaults

- Submit each signed accounting report once per accounting epoch, rather than
  repeatedly refreshing it every 100 blocks.
- Poll for status and accounting every 60 seconds.
- Allow 25 blocks of publication grace before a missing-feed burn fallback.
- If a valid report arrives later in the same epoch, replace the burn fallback
  once, respecting the 100-block minimum gap and any higher chain rate limit.

Finalized commits may still await automatic reveal while a newer epoch is
submitted. Uncertain transactions remain blocked until reconciled. Neither a
finalized commit nor a signed accounting report proves that alpha was received.

## Compatibility and safety

- Existing journals and authorizations remain in use; never delete/reset them to
  upgrade. The epoch guard also recognizes earlier block-mode submissions.
- `--weight-cadence block` preserves v0.2.0's repeated refresh mode.
- `--missing-accounting wait` disables the missing-feed burn fallback.
- Finney remains the default RPC, with explicit RPC override support.
- Invalid signatures, conflicting reports and network errors still block; they
  are never converted into fake zero-work accounting.
- The signed aggregate feed schema is unchanged. The separately deployed owner
  finalizer carries eligible late approvals into future accounting reports;
  this release does not approve jobs or recover already-burned emissions.

## Updating Docker

For the repository's Compose deployment:

```bash
docker compose pull validator
docker compose up -d --no-deps validator
```

Keep the same state volume, wallet mount and explicit signing configuration.
Use a single signing instance per hotkey. Do not start a second validator while
the old process is still shutting down. Operators using a pinned image must first
select `ghcr.io/everyframe-studio/everyframe-validator:v0.3.0` or its release digest.

The stable `latest` tag is updated after successful publication; it does not
automatically replace running containers. This release does not change the live
owner validator or authorize third-party signing. Linux amd64 image; wheel and
source archive included. No PyPI publication.
