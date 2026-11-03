# Treasury wallet

Validator v0.8.3 rotates the unused miner allocation across the registered UIDs
of **one non-owner treasury wallet coldkey**. Installing this code does not activate
funding. The treasury wallet coldkey and future activation epoch must be configured first.

## How a UID is selected

For each signed chain-epoch assignment, the publisher and every validator:

1. Read the metagraph and the pool coldkey's `OwnedHotkeys` at the same finalized
   epoch-start block. Only UIDs registered under that coldkey are eligible.
2. Sort candidates by UID and hotkey, then hash the candidate set.
3. Derive a seed from the chain/network, epoch-start block hash, accounting report
   hash, pool coldkey and candidate hash. Deterministic rejection sampling selects
   one UID without modulo bias.
4. Verify the same choice independently. The assignment contains the chosen
   UID/hotkey, pool coldkey, candidate count/hash, seed and snapshot provenance.
5. Before signing and when observing the reveal, recheck the selected identity.
   A changed or deregistered target blocks submission; it never causes a local
   reroll of this epoch.

This is reproducible pseudorandom selection, not secret randomness. All validators
choose the same UID for a report. Consecutive epochs may select the same UID. A
single registered UID is valid but offers no rotation; new UIDs enter the next
snapshot. Public data cannot prevent a copier from reconstructing the selection.

## Allocation

Approved serving fees receive a **10–50% quality bonus**, subject to the available
miner budget. Quality uses seven days of provider coverage and reliability;
fewer than five attempts receives the base rate. Three providers with at least
95% successful attempts earns the maximum. Sealed historical reports retain their
original rules. Eligible reviewed historical shortfalls then use remaining
capacity without replaying claims already allocated.

Only the remainder goes to the selected treasury wallet UID. The original serving
weights stay unchanged. The pool allocation is an actual payout, not a burn,
completed API work, or a change to owner/validator emissions. It is recorded as
`contestPoolBudgetUnits`, separately from serving earnings. Pool-coldkey UIDs
cannot also claim serving rewards in the same report. An explicit
`scheduled_reuse_pool` slot funds the pool without paying a report twice.
An absent report or an empty/invalid candidate set does not create an allocation.

Assigned weight is not proof of received alpha or a guaranteed USD amount.
Awards require a separate approval and payment process; validators do not spend
treasury funds automatically.

## Activation

1. Register treasury wallet hotkeys under the chosen **non-owner coldkey** and publish
   that coldkey address. No fixed hotkey or manual UID list is required.
2. Use v0.8.1 or later for the renamed commands below. Existing v0.8.0
   participants remain protocol-compatible and can use the legacy command alias.
   Each operator explicitly accepts the coldkey:

   ```bash
   everyframe-validator accept-treasury-wallet --state-dir ./state/mainnet117 \
     --coldkey TREASURY_WALLET_COLDKEY --yes
   ```

   With Docker use the same command through `docker compose exec validator`,
   using `--state-dir /state`. This does not grant or renew signing consent.
3. Confirm participant readiness, then configure the publisher:

   ```text
   --treasury-wallet-from-chain-epoch FUTURE_CHAIN_EPOCH
   --treasury-wallet-coldkey TREASURY_WALLET_COLDKEY
   ```

The first activation must be in the future; the candidate set is verified before
pinning it. `contest-pool-activation.json` is durable and written before public
assignments. Removing the flags cannot disable it. Readers pin the accepted
coldkey and refuse a downgrade after v4 activation. Changing the treasury wallet coldkey
requires a separately coordinated migration.

Historical v1/v2 burn assignments and v3 fixed-recipient assignments remain
readable for existing commitments. The legacy `--hotkey` option is retained only
for an already configured v3 pool. New deployments use coldkey-only v4.

## Compatibility

Treasury wallet is the user-facing name. Existing `prize_pool` / `contestPool`
protocol identifiers, signed assignment policies, consent files and activation
markers keep their original names. Existing consent remains valid.
`accept-contest-pool` and `--contest-pool-*` remain supported aliases for older
operator scripts. This terminology update does not change rewards or activate
treasury funding.
