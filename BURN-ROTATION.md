# Deterministic owner burn selection

Available in 0.6.0. Version 0.5.1 and older readers do not support assignment
v2. Installing this code does not activate a new writer policy or restart a
validator. Do not switch the publisher until all participating validators have
compatible code and have passed read-only checks.

## Selection

The owner publisher uses the same finalized `chainEpochStart` block and hash as
the canonical assignment. At that block it reads `SubnetOwner`, the metagraph
and `OwnedHotkeys`. All registered UID/hotkey pairs belonging to that coldkey
are candidates. Conflicting chain mappings, empty sets, duplicate identities and
unavailable historical state are errors. No local allowlist or fabricated miner
is substituted.

Candidates are sorted by UID, then hotkey. SHA-256 of their canonical JSON is
`candidateHash`. The seed is SHA-256 of canonical JSON containing the policy,
network, netuid, genesis, actual chain epoch and start block/hash, accounting
epoch, assignment mode, report payload hash, and candidate hash. A reuse-burn
assignment has a null payload hash and its explicit mode as input.

Convert the seed to an unsigned 256-bit integer. For N candidates, reject values
at or above `2^256 - (2^256 mod N)`; on rejection use SHA-256(seed bytes followed
by an unsigned 64-bit big-endian counter, beginning at 1). The accepted value
modulo N selects the candidate. This is deterministic pseudo-random selection,
not secret randomness, and consecutive epochs may choose the same UID.

The v2 signed assignment carries `burnSelection`: policy, snapshot block/hash,
owner coldkey, candidate hash/count, seed hash, selected UID and hotkey. Each
validator independently reconstructs it from the finalized chain before using
it. A private immutable verification marker is bound to the full assignment.
The current UID/hotkey/owner are rechecked before signing and at reveal/application.
Changes stop the submission; they never trigger a different choice within the
same assignment.

Only the unused allocation moves. Approved miner work, quality uplift, recovery
amounts, total weight budget and chain parameters remain unchanged. When only
one candidate exists there is no effective rotation; when the work fills the
pool there is no unused allocation to move.

## Coordinated activation

1. Upgrade the owner runtime, independent readers and assignment publisher.
   Keep the same state, wallets, RPC configuration and pending commitments.
2. Verify reader health and compare dry-run vectors before choosing a future
   actual chain epoch. The epoch is `SubnetEpochIndex`, not an accounting ID.
3. Add `--burn-rotation-from-chain-epoch N` to the assignment publisher service.
   Until N it produces v1; from N it produces v2. The publisher stores its
   activation in `burn-activation.json` before writing a new assignment, so a
   restart without the flag cannot silently revert. A per-output process lock
   serializes publishers. Conflicting settings and existing assignments are
   rejected. Do not delete the lock or policy marker to retry a failed run.
4. Readers pin the new policy after their first verified v2 assignment. Later
   v1 assignments at or after that point are rejected. Do not delete these
   markers or old journals to bypass a deployment error.

Legacy assignments and already-submitted records keep their original target.
Rollback after activation requires a coordinated policy decision, not restarting
an old writer or changing an existing public assignment.

## Effect on copying

The selection can make a previous on-chain vector stale when unused allocation
and multiple owner UIDs exist. It does not guarantee low VTrust: the input report
and selection are public, so anyone following the current protocol can compute
the same vector. It is not protection against fresh public-feed reconstruction.

Changing burn recipients requires confirming the active chain runtime treats
these owner-associated hotkeys as burn/recycle recipients. The reference
Subtensor coinbase implementation discovers them from the owner coldkey;
runtime compatibility must be reviewed before production activation.
