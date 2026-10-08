# Everyframe Validator

Standalone validator for **Everyframe mainnet SN117**, with explicit testnet SN566 support.
External operators download signed finalized accounting, calculate weights locally,
and submit with their **own** Bittensor hotkey. No owner SSH, private accounting DB,
provider API keys, GPU, Phala account, or miner TEE is required.

## Release status

Version 0.1.0 has release automation for GitHub assets and a public GHCR image;
it is **not published to PyPI**. Workflow files alone do not mean a release has
run or a validator is deployed. Check the repository's Releases and Actions
pages for publication status. See [RELEASING.md](RELEASING.md).
The default feed URLs are reserved integration targets: the coordinator must
publish real signed finalized epochs there before validators can score work.
A missing epoch produces `waiting`; it never becomes a fabricated zero-work epoch.
This release does not activate mainnet mining, register a wallet, or spend TAO.

## Trust model

```text
Everyframe reviewed accounting → owner-side finalizer → signed public epoch JSON
                                                           ↓
External validator: verify signer + chain + epoch → compute weights → own hotkey
```

Like SayGM, validators trust the operator's finalized accounting for genuine paid
demand, cost attribution and the supplied price/emission snapshot. They do not
re-run models, re-price provider invoices, or independently prove customer payment.
Signatures authenticate the publisher; they do **not** make its claims trustless.
Two conflicting signed versions are rejected locally, but this release has no
global transparency log to detect a publisher equivocating across fresh validators.

The validator independently checks chain genesis, current epoch/tempo, historical
boundary hashes, registered miner identities, validator permit, owner/burn identity,
weight constraints, cooldown, and identity stability immediately before signing.
TEE execution verification stays in the existing worker/coordinator path.

## Install from this directory

Linux, Python 3.10–3.14, reliable network access, and a registered validator hotkey
with an active validator permit are required for signing. Registration alone does
not grant a permit. Your wallet must have enough funds for applicable chain fees.

```bash
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/everyframe-validator --help
```

Do not run validators as root. No dependency on the sibling `everyframe-subnet`
directory exists. Bittensor is pinned to 11.1.0; use a reviewed environment/image
for production and review dependency updates before installing them.

Initialize a separate owner-only state directory for each network and hotkey:

```bash
everyframe-validator init \
  --state-dir ./state/mainnet117 \
  --validator-hotkey YOUR_PUBLIC_SS58 \
  --wallet YOUR_WALLET_NAME \
  --wallet-hotkey YOUR_HOTKEY_NAME \
  --wallet-path /absolute/path/to/wallets

everyframe-validator run --state-dir ./state/mainnet117 --once
```

Mainnet is the default. Add `--network testnet` **to init** and choose a separate
state directory for SN566. Runtime reads the pinned profile; there is no runtime
network override. Never put a mnemonic, seed, provider key, or coldkey secret into
command-line arguments or this repository. The CLI takes only a public hotkey and
wallet file location. An encrypted hotkey can use `--wallet-password-file` pointing
to mode-0600 JSON with a `hotkey` password field. Protect that file like a key.
Coldkey signing is never requested. Configure wallet paths/password paths as
absolute paths, especially when running from systemd or Docker.

### Explicit signing

The following commands authorize and then enable real chain transactions. They are
for the validator operator to run deliberately—not installation steps:

```bash
everyframe-validator authorize --state-dir ./state/mainnet117 \
  --days 30 --max-submissions 1000 --yes
everyframe-validator run --state-dir ./state/mainnet117 --submit
```

This is **your local consent**, not subnet-owner approval. Consent expires and is
bounded by submission count; renewing it does not clear pending submissions or
allow replaying old epochs. Without `--submit`, even an authorized profile stays
read-only with respect to the chain. Per-transaction SDK fee policy is capped at
0.02 TAO; this is a safety ceiling, not an expected fee or income guarantee.
Keep the process running to reconcile commits/reveals. Epochs come from finalized
chain state; only the latest closed epoch is eligible, never backlog replay.

### Docker

After publication, Linux amd64 operators can use a prebuilt image without
building or installing Python. Download `image.txt` and `SHA256SUMS` from a
GitHub Release, verify the checksum, and use its immutable digest:

```bash
sha256sum --check --ignore-missing SHA256SUMS
docker pull "$(cat image.txt)"
docker run --rm --network none "$(cat image.txt)" --version
```

For persistent dry-run deployment, follow [the operator guide](deploy/README.md).
Releases do not update running validators automatically; no `latest` tag is used.

To build from source instead:

```bash
docker compose build
docker compose run --rm validator init --state-dir /state \
  --validator-hotkey YOUR_PUBLIC_SS58 \
  --wallet YOUR_WALLET_NAME --wallet-hotkey YOUR_HOTKEY_NAME --wallet-path /wallets
docker compose up -d
docker compose logs -f validator
```

The named volume is initialized for UID/GID 10001. The default compose service is
**dry-run**, non-root, read-only except its private state, and survives restarts.
For signing, initialize with wallet flags, add a read-only wallet mount, run the
authorization command inside the container, and explicitly add `--submit` to its
command. Give UID 10001 access to the selected hotkey without making it public.
For encrypted keys, mount the password JSON read-only with matching ownership.
Wallet flags are stored during init but no wallet is opened in dry-run mode. If
you omitted them initially, stop the service and edit only the `wallet`,
`walletHotkey`, `walletPath` and `walletPasswordFile` fields in private `config.json`;
keep the validator identity, trust pins and journals unchanged.
Do not mount unrelated wallets or the owner's server credentials. The supplied
systemd unit under `deploy/` is an alternative template, also dry-run by default.

## Operations

```bash
everyframe-validator status --state-dir ./state/mainnet117
everyframe-validator doctor --state-dir ./state/mainnet117
everyframe-validator reconcile --state-dir ./state/mainnet117
```

`status` can run while the validator is busy: it reads committed journal data and
the last completed iteration without taking the signing-operation lock. The last
iteration's age is shown explicitly; it is not a live heartbeat or signing permission.
`doctor` checks the profile, journal, public wallet identity, local authorization,
RPC/permit and signed feed without unlocking a private key or sending a transaction.
Use `doctor --offline` to skip network checks. Missing authorization is a warning
for dry-run operation. `signingPrerequisitesOk` reports the checks performed, not
a guarantee that signing will succeed; offline mode cannot establish live readiness.

- `dry_run`: authenticated accounting and chain checks produced a plan; no signature.
- `waiting`: the newest closed epoch is not published; no substitute epoch or burn.
- `pending_reveal`: a finalized commit exists, but the weights are not yet proven revealed.
- `finalized`: non-commit-reveal submission confirmed; awaiting read-back.
- `awaiting_readback`: waiting for finalized non-commit-reveal weights to be read back.
- `blocked`: unresolved submission; no new signing.
- `chain_failed`: an uncertain transaction was found finalized with a dispatch failure;
  it remains in the journal and is not resubmitted for the same epoch.
- `already_submitted`: persistent epoch deduplication prevented another submission.
- `failed`: verification, chain access, authorization, or configuration failed.

Normal reveal/read-back waiting is not an error (`run --once` exits successfully).
Unresolved uncertainty returns exit code 2 from `run --once` or `reconcile`.
Reconciliation reports include the inclusion block and, when available, pending
commit reveal scheduling supplied by the chain. They do not claim earned alpha.

JSON status goes to stdout and `state-dir/status.json`; errors use allowlisted
codes and actionable messages rather than raw exception text that could leak
secrets. Detailed committed plans are in `journal.db`. Both state
databases and `identity.json` are mandatory after init—missing state is not silently
recreated. Back up the **entire** state directory consistently while stopped.
Do not share one state directory between hotkeys or run two machines with copied
state and the same signing key. The lock protects one local filesystem only.

For `unknown`/`submitting` after a crash, automatic retry is intentionally disabled.
New submissions durably record their exact signed transaction hash **before**
broadcast; signed transaction bytes are not stored. `reconcile` searches finalized
blocks for that exact hash, at most 20 blocks per iteration with persisted progress.
Confirmed success enters normal reveal/read-back reconciliation; confirmed failure
becomes `chain_failed`. Neither path resubmits the transaction. Ordinary `run`
iterations perform the same recovery checks.

A missing transaction is **not** proof that it is safe to retry. Older journal
entries without a saved hash remain blocked with `transaction_hash_unavailable`
and require manual chain investigation. There is deliberately no `force retry` or
`clear pending` command. Never delete the journal to make it proceed.

Reconciliation checks exact SDK-quantized weights, finalized chain identities,
owner identity, `LastUpdate`, absence of newer pending commits, and a matching
timelocked reveal event. An identical old vector alone is not reveal proof.

## Publish accounting (subnet operator only)

The existing Everyframe finalizer produces the sealed `reward-epochs.db`.
The included adapter reads that database **read-only** and emits a privacy-safe
public aggregate with signed miner-hotkey bindings. It never copies the SQLite
database, individual job IDs, raw receipts, prompts, outputs, tokens, or proofs.

```bash
everyframe-publish-epoch --network mainnet \
  --epochs /absolute/path/to/reward-epochs.db \
  --epoch CLOSED_EPOCH_NUMBER \
  --bindings /absolute/path/to/approved-bindings.json \
  --signing-key /absolute/path/to/coordinator.pem \
  --output /absolute/path/to/public-validator-epochs
```

The signing PEM must be owned by the current operator and mode 0600. The tool
checks its public key against the pinned network signer. `--expected-public-key`
is an explicit reviewed override, not automatic trust on first use. For a separate
accounting signer, distribute that pinned public key to validators out of band and
use their init `--public-key` override. Do not copy private signing keys to a web root.

Serve **only** these JSON files, read-only over HTTPS, at:

- Mainnet: `https://subnet.everyframe.studio/mainnet/v1/validator/epochs/{epoch}.json`
- Testnet: `https://subnet.everyframe.studio/v1/validator/epochs/{epoch}.json`

The public path must map to the matching network's output directory. Return 404
for unpublished epochs; never rewrite it to a dashboard HTML response. Do not
expose `/control`, databases, or key files. Invoke the publisher after successful
finalization; publish failures must alert the operator. File creation is atomic and
immutable: re-publishing identical content is safe; revising it fails. A malformed,
unreviewed or missing epoch must stop publication, not generate an empty epoch.

No cloud credentials are needed by readers. Validators pin the signing key, bound
response size, disallow redirects/proxies/private IP destinations, require the exact
latest epoch and reject conflicting signed data. Endpoint/signer changes require a
reviewed new profile, preserving old journals; do not edit trust pins live.

See [PROTOCOL.md](PROTOCOL.md) and [CORE-PROVENANCE.md](CORE-PROVENANCE.md).

## Test

```bash
.venv/bin/pip install '.[test]'
.venv/bin/pytest -q
.venv/bin/python -m build
```

Tests use generated test signing keys, synthetic accounting, and mocked chain/wallet
submission. They incur no provider charges, registration burns, or weight transactions.
A production rollout still requires a live publisher, a separate testnet validator
canary with finalized reveal verification, then explicitly authorized mainnet rollout.

## License

Apache-2.0. See [LICENSE](LICENSE) and [LICENSE-NOTICE.md](LICENSE-NOTICE.md).
