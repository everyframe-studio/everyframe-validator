# AI Film Contest Pool

Validator v0.7.0 supports a separately identified contest-pool recipient in signed
assignment v3. Installing the code does not activate this allocation.

The owner accounting policy pays approved serving fees with a **10–50% quality
bonus**, subject to the available miner budget. Quality uses the previous seven
days of provider coverage and reliability; fewer than five attempts receives the
base rate. Three providers and at least 95% successful attempts earns the maximum.
Existing reports retain their original bonus rules. Eligible reviewed historical
shortfalls then fill remaining capacity without replaying already allocated claims.
Only the remainder of the miner allocation goes to the contest pool. Subnet owner
and validator emissions are not redirected by this policy.

The pool is an actual non-owner recipient, **not a burn address or a serving
miner**. Its allocation is recorded separately as `contestPoolBudgetUnits`, never
as completed jobs or API earnings. Amounts describe assigned weights; they are not
proof of received alpha or a guaranteed USD payout. Awards require a separate
approval and payment process; this feature does not spend the pool automatically.

## Before activation

1. Register the pool miner under a non-owner coldkey. Publish both public SS58
   addresses and verify their registered UID at a finalized block.
2. Upgrade the owner reader, assignment publisher and independent validators.
3. Each independent operator explicitly accepts the published identity:

   ```bash
   everyframe-validator accept-contest-pool --state-dir ./state/mainnet117 \
     --hotkey POOL_HOTKEY --coldkey POOL_COLDKEY --yes
   ```

   In Docker, use the same command through `docker compose exec validator` with
   `--state-dir /state`. This does not grant or renew transaction authorization.
4. Confirm readiness, then set all three publisher arguments:

   ```text
   --contest-pool-from-chain-epoch FUTURE_CHAIN_EPOCH
   --contest-pool-hotkey POOL_HOTKEY
   --contest-pool-coldkey POOL_COLDKEY
   ```

Both `everyframe-publish-assignment` and the systemd assignment-service module
accept these arguments. There is no default pool identity. The initial activation
must be in the future and the registered identity is checked before it is pinned.
The publisher persists `contest-pool-activation.json` before exposing assignments.
Removing the command-line flags does not disable an activated policy or change its
recipient. A new recipient or policy change needs a coordinated migration.

## Verification and accounting

Every v3 assignment includes the exact pool UID, hotkey, coldkey, subnet owner and
finalized epoch-start block/hash. Readers independently query that block and verify
the hotkey owner. They check the identity again against fresh chain state before
signing, and while observing the reveal/application. Re-registration, ownership
change, self-payment, malformed metadata, duplicate identities or missing consent
blocks submission. Readers retain v1/v2 support for historical commitments and
reject a v3-to-v2 downgrade after activation.

Serving weights use the same cap/quantization as v2. Only the former burn remainder
changes recipient. An explicit `scheduled_reuse_pool` slot funds the pool without
paying the same accounting report twice. An absent feed still waits; it never
creates a pool allocation. Missing, invalid or unapproved pool identity also waits
for operator correction rather than silently sending funds elsewhere.

No historical signed report, transaction or journal is rewritten. Always retain
state when upgrading. A pool allocation in a journal is not evidence that the
recipient has already received it; confirm on-chain emissions separately.
