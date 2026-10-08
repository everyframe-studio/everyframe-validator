# Operator canary deployment

This optional workflow runs a separate **dry-run validator** without wallet
mounts or signing authorization. It does not touch existing signing services,
third-party validators or miners. Use a separate host/state from a signing
validator; never copy its journals into this canary.

## Prepare one host per environment

Use Linux amd64, Python 3.10+, Docker Engine and Compose v2 supporting
`up --wait --wait-timeout`. Allow disk space for two images and persistent
state. Docker permissions are effectively root: use a dedicated deployment
account and tightly protect its SSH key.

1. Install the reviewed `deploy-release.py` and this directory's `compose.yaml`
   at `/opt/everyframe-validator-deploy`. Set directory ownership to the deploy
   account and mode 0700. Do not use the root-level source-build Compose file.
   Rollouts never download or replace deployment scripts automatically.
2. Download `image.txt` and `SHA256SUMS` from a published Release, verify them,
   and initialize an independent profile:

   ```bash
   sha256sum --check --ignore-missing SHA256SUMS
   cd /opt/everyframe-validator-deploy
   export VALIDATOR_IMAGE='ghcr.io/everyframe-studios/everyframe-validator@sha256:RELEASE_DIGEST'
   docker compose --project-name everyframe-validator-canary --file compose.yaml pull
   docker compose --project-name everyframe-validator-canary --file compose.yaml run --rm validator init \
     --state-dir /state --network mainnet --validator-hotkey YOUR_PUBLIC_HOTKEY
   ```

   For testnet use `--network testnet`. State lives in the named volume
   `everyframe-validator-canary_validator-state`, owned by container UID 10001.
   Do not rerun init on initialized state or delete journals to fix errors.
3. Ensure the matching signed finalized-epoch feed is published and reachable.
   `waiting` is safe runtime behavior, but is not deployment readiness.
4. Create GitHub environments `validator-staging` and `validator-production`.
   Require reviewers, prevent self-review/admin bypass, and allow only `main`.
   Add the following **environment-scoped** secrets:

   | Secret | Value |
   | --- | --- |
   | `DEPLOY_HOST` | Hostname or IPv4 address, SSH port 22 |
   | `DEPLOY_USER` | Dedicated deployment user |
   | `DEPLOY_SSH_KEY` | Its private SSH key, never a wallet key |
   | `DEPLOY_KNOWN_HOSTS` | Verified OpenSSH known_hosts entry |

   Obtain the host key through a trusted channel, not an unauthenticated scan
   in the workflow. If your GitHub plan lacks required reviewers, do not enable
   remote credentials: use a manually reviewed deployment on the host instead.

## Deploy and verify

Run **Deploy validator canary** from `main`, choose the release/environment,
and approve it. Or, after manual review on the host:

```bash
python3 /opt/everyframe-validator-deploy/deploy-release.py "$VALIDATOR_IMAGE"
```

The script locks deployment, pulls before stopping the old container, and waits
up to 480 seconds for a fresh successful iteration from the new container.
Missing feed, stale status, failed iterations or blocked journals cannot pass.
It never runs two versions concurrently against the same state.

Runtime health checks continue after deployment. Docker does not automatically
restart/roll back a merely unhealthy container; monitor health and status.

```bash
cd /opt/everyframe-validator-deploy
export VALIDATOR_IMAGE="$(cat current-image.txt)"
docker compose --project-name everyframe-validator-canary --file compose.yaml ps
docker compose --project-name everyframe-validator-canary --file compose.yaml exec validator \
  everyframe-validator status --state-dir /state
```

## Rollback and interruptions

A failed update stops the candidate and restores the previous image. A failed
first deployment stays stopped. Journals are never rewound and volumes never
removed. The workflow remains failed even when rollback succeeds.

The host writes `deployment-pending.json` before switching and
`current-image.txt` after success. If interrupted or rollback fails, the pending
marker blocks retries. Inspect actual container image, health and journal;
finish restoring the known-good image, reconcile the current-image record,
and archive the pending marker only after confirming the result. Never delete
state or run a duplicate validator to unblock deployment.

Manual rollback uses the same workflow with an earlier published release.
Review state-schema compatibility first. Old code may not read newer state;
stop and investigate rather than restoring an old journal snapshot.

Signing requires a separately reviewed deployment and the root README's
explicit wallet/authorization steps. This managed canary cannot enable it.
