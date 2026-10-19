# EveryFrame Validator

Standalone validator for **EveryFrame mainnet SN117**, with explicit testnet SN566 support.
External operators download signed finalized accounting, calculate weights locally,
and submit with their **own** Bittensor hotkey. No owner SSH, private accounting DB,
provider API keys, GPU, Phala account, or miner TEE is required.

## Release status

Version 0.3.0 provides a Linux amd64 image at
`ghcr.io/everyframe-studio/everyframe-validator:latest` and versioned tags.
It is **not published to PyPI**. Check [Releases](https://github.com/everyframe-studio/everyframe-validator/releases)
for published versions; see [RELEASING.md](RELEASING.md).
The source branch includes a newer chain-epoch scheduling fix (not yet a tagged
release). It uses Finney RPC, at most one allocation per actual chain epoch,
60-second polling, a 100-block minimum submission gap, and a durable signed-report
queue. v0.3.0's accounting-epoch cadence and v0.2.0's block refresh are superseded
in this source version; check the release tag before assuming an image includes it.
Invalid signatures and network errors still block. No fake zero-work accounting
is created. Installation never registers a wallet or authorizes signing.

## Trust model

```text
EveryFrame reviewed accounting → owner-side finalizer → signed public epoch JSON
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

## Docker-based installation (recommended)

Use the prebuilt image. No Python, source build, or `image.txt` is needed.
`--pull always` fetches the latest published stable image when the command runs:

```bash
docker pull ghcr.io/everyframe-studio/everyframe-validator:latest
docker run --rm --network none ghcr.io/everyframe-studio/everyframe-validator:latest --version
docker volume create everyframe-validator-state
docker run --rm --pull always \
  -v everyframe-validator-state:/state \
  ghcr.io/everyframe-studio/everyframe-validator:latest init \
  --state-dir /state --validator-hotkey YOUR_PUBLIC_SS58
docker run -d --name everyframe-validator --pull always --restart unless-stopped \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:mode=1777,size=256m \
  -e BITTENSOR_RUNTIME_CACHE_DIR=/state/runtime-cache \
  -v everyframe-validator-state:/state \
  ghcr.io/everyframe-studio/everyframe-validator:latest run --state-dir /state
docker logs -f everyframe-validator
```

This starts a **dry-run**, not a signing validator. Follow [Explicit signing](#explicit-signing)
and the wallet-mount instructions below before enabling `--submit`.
Do not rerun `init` on an initialized volume or delete it to resolve errors.
`latest` does not hot-replace an already running process: pull and recreate the
container deliberately with the **same state volume**, stopping the old instance
first. Review changes before upgrading a signing validator.

If using this repository's root Compose file, it defaults to the latest image:

```bash
docker compose run --rm validator init --state-dir /state --validator-hotkey YOUR_PUBLIC_SS58
docker compose up -d
# Later, update the existing container without deleting its state:
docker compose pull
docker compose up -d
```

Use either the standalone Docker commands or Compose, not both with the same hotkey.
Pin `:v0.3.0` or an immutable `@sha256:...` for reproducibility. The separate
[managed canary workflow](deploy/README.md) still requires immutable release
digests; `image.txt` remains available for that advanced workflow only.

### Optional: build the Docker image locally

If you prefer to build the image from this repository:

```bash
docker build -t everyframe-validator:local .
export VALIDATOR_IMAGE=everyframe-validator:local
export VALIDATOR_PULL_POLICY=never
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

## Install from source (alternative)

Linux, Python 3.10–3.14, reliable network access, and a registered validator hotkey
with an active validator permit are required for signing. Registration alone does
not grant a permit. Your wallet must have enough funds for applicable chain fees.

Run these commands from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
everyframe-validator --help
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
read-only with respect to the chain. The default block-paced mode has a zero-fee
policy; a nonzero/unavailable fee estimate blocks signing. The legacy epoch mode
retains its 0.02 TAO ceiling. Neither is an income guarantee.
Keep the process running to recover uncertain transactions. Only the latest
closed accounting epoch is eligible, never backlog replay.

## Operations

```bash
everyframe-validator status --state-dir ./state/mainnet117
everyframe-validator doctor --state-dir ./state/mainnet117
everyframe-validator reconcile --state-dir ./state/mainnet117
```

### RPC endpoint

Mainnet uses the SDK's **Finney** endpoint by default; testnet uses its test
endpoint. All paths verify the expected genesis before trusting chain data.
Choose an alternative when your RPC lacks historical state:

```bash
everyframe-validator run --state-dir ./state/mainnet117 --rpc-url wss://rpc.example.org
# Or apply the override to run, doctor and reconcile:
export EVERYFRAME_RPC_URL=wss://rpc.example.org
everyframe-validator doctor --state-dir ./state/mainnet117
```

For Docker pass `-e EVERYFRAME_RPC_URL=wss://rpc.example.org`; Compose reads the
same variable from the shell or a private `.env`. Overrides change only the
transport, never the network, hotkey, accounting signer or journal identity.
Use `wss://` without URL credentials/query parameters; `ws://` is allowed only
for localhost nodes. Historical-state errors fail closed, never clear journals.

For an authenticated provider such as Blockmachine, use
`EVERYFRAME_RPC_URL=wss://rpc.blockmachine.io` and either
`EVERYFRAME_RPC_API_KEY` or `EVERYFRAME_RPC_API_KEY_FILE` (not both).
The key is sent only as an Authorization header to the explicitly selected
endpoint. Never put keys in command arguments or Git. A key file must contain
only the key, be owned by the validator user (container UID 10001), have mode
0600, and be mounted read-only. In Compose, add the file mount and
`EVERYFRAME_RPC_API_KEY_FILE` environment entry when using that option.
There is no silent fallback that could send a key to another endpoint.

### Chain-epoch scheduling and missing-accounting burn

These are the defaults, shown explicitly:

```bash
everyframe-validator run --state-dir ./state/mainnet117 --submit \
  --interval 60 --weight-cadence chain --weight-interval-blocks 100 \
  --accounting-grace-blocks 25 --missing-accounting burn
```

Status polling and weight submission are separate. `--interval` is the pause in
seconds between checks; `--weight-interval-blocks` is a **minimum finalized-block
gap**. The chain's rate limit takes precedence if higher. Signing is limited to
one early slot per finalized `SubnetEpochIndex` / `LastEpochBlock`, not the
fixed `tempo+1` accounting window. The schedule is rechecked before broadcast.
Distinct chain epochs and sufficiently separated timelock rounds prevent a
reward allocation from being overwritten by a same-window burn. Unsupported
short tempos or rate limits block instead of weakening these safeguards.

A missing feed gets 25 blocks of publication grace (bounded to the epoch length).
With `burn`, an absent queued epoch
(HTTP 404) produces a full-weight allocation to the verified subnet-owner burn
UID, labelled `missing_epoch_burn`. It never creates a fake signed/zero-work
artifact. Signature failures, conflicting accounting, timeouts, and other HTTP
failures still block submission. Set `--missing-accounting wait` to disable
the burn fallback. A later signed report is queued for a **different chain epoch**;
it never replaces a burn inside the same reveal window. A durable cursor processes
reports in order and does not skip reports during outages. On first adoption it
starts at the latest closed accounting window or after already-submitted work,
whichever is later. Older missed/submitted work is not automatically replayed.
When the queue is caught up, `standby_burn` prevents retaining a paid allocation
indefinitely; it does not consume a signed report. `doctor`
continues to report an absent feed; that describes
accounting availability even when the run loop is configured to burn.

Finalized commits may queue while earlier commits await the chain's reveal
delay, but never share a chain-epoch slot. They are initially recorded as
`commit_finalized`, **not** `revealed`. Finalized archive observations separately
verify removal of the exact commitment, its reveal event and weights, then its
vector at the first subsequent mechanism step. `application_observed` means
weight application evidence, **not proof of alpha settlement**. Ambiguous,
expired or overwritten allocations become `application_needs_review`; they are
not automatically replayed. Chain pending-commit counts are
checked, and uncertain broadcasts still block all further submissions until
exact transaction recovery succeeds. The existing authorization expiry and
submission-count cap remain in force; faster cadence consumes that cap sooner.
No chain hyperparameters are changed. Old `--weight-cadence epoch` and `block`
arguments are compatibility aliases for `chain`, not a way to restore unsafe
refreshes. Existing commitments cannot be cancelled by stopping the process.
Preserve journals, the new `reward-schedule.json` cursor, and authorizations.

`status` can run while the validator is busy: it reads committed journal data and
the last completed iteration without taking the signing-operation lock. The last
iteration's age is shown explicitly; it is not a live heartbeat or signing permission.
`doctor` checks the profile, journal, public wallet identity, local authorization,
RPC/permit and signed feed without unlocking a private key or sending a transaction.
Use `doctor --offline` to skip network checks. Missing authorization is a warning
for dry-run operation. `signingPrerequisitesOk` reports the checks performed, not
a guarantee that signing will succeed; offline mode cannot establish live readiness.

- `dry_run`: chain checks and signed accounting (or the configured missing-epoch burn fallback) produced a plan; no signature.
- `waiting`: cooldown, pending-commit limit, or an absent epoch with `--missing-accounting wait`; check `reason`.
- `commit_finalized`: submission finalized; it is not proof of reveal.
- `application_observed`: weights verified across a mechanism step; not proof of alpha payment.
- `application_needs_review`: expired, ambiguous or overwritten allocation; no automatic reward replay.
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

The existing EveryFrame finalizer produces the sealed `reward-epochs.db`.
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
immutable: re-publishing identical content is safe; revising it fails. Malformed
accounting must stop publication, not generate an empty epoch.

The updated owner finalizer recognizes approved, eligible work once using its
private sealed-epoch provenance as the deduplication ledger. Review-held work is
deferred, without blocking unrelated approved work. An approval made at or after
the close belongs to a later report. Older approved work never recognized in a
sealed report is carried forward; previously recognized jobs/attempts cannot be
counted twice, including across upgrades and restarts. No job is auto-approved,
and disputed or ineligible work is not made eligible by this change.

Public scores describe **work recognized in that report**, not necessarily jobs
completed during that same wall-clock window. The signed aggregate format is
unchanged; original timestamps and job proofs stay private. Validators still
authenticate owner accounting rather than independently reviewing each job.
Recognition is not a guaranteed USD payment or proof of alpha received; emission
caps, consensus, and commit/reveal still apply. Already-burned emissions are not
recoverable. Monitor finalization and actual publication separately: a sealed
report is not evidence that validators fetched or submitted it.

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
