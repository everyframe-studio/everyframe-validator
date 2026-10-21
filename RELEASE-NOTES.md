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
