# Optional automatic validator updates

The host-side updater supports **Linux Docker Compose deployments** of the
official validator image. Python/venv and PM2 installations continue using manual
upgrades; the updater does not rewrite those services. Auto-update is off until the
operator installs it and opts in. It never enables signing or accepts a contest
pool identity.

Use the `auto-update.py` asset from the official GitHub Release. Download its
`SHA256SUMS`, verify the checksum, and place the reviewed script on the host.
Do not run this tool inside the validator container or mount a Docker socket there.
The release also contains `auto-update.json` describing the supported state format
and assignment policies. Older releases without that manifest cannot be installed
automatically.

## Setup

Keep your validator running. Pass its actual Compose file and **existing project
name** (see `docker compose ls`). The Compose file must match the running service,
including its command, mounts, environment and healthcheck. If you use override
files, first produce a private merged Compose file and verify it matches the
running service. Keep the updater directory outside the validator state directory.

```bash
python3 auto-update.py setup --root /opt/everyframe-validator-updater \
  --compose-file /absolute/path/compose.yaml --project YOUR_EXISTING_PROJECT --yes
python3 /opt/everyframe-validator-updater/auto-update.py check \
  --root /opt/everyframe-validator-updater
```

`setup` captures the existing configuration, image digest and version. It refuses
a different running configuration, multiple replicas, mutable/unofficial source
images, missing persistent state mount or a missing standard healthcheck. The
saved Compose snapshot is private because resolved environment values may contain
RPC credentials. Later edits to the original Compose file are not imported
automatically; review and set up a fresh updater directory to adopt them.

Run one update manually before enabling the timer:

```bash
python3 /opt/everyframe-validator-updater/auto-update.py tick \
  --root /opt/everyframe-validator-updater
sudo python3 /opt/everyframe-validator-updater/auto-update.py install-timer \
  --root /opt/everyframe-validator-updater --yes
```

The optional systemd timer checks hourly with up to ten minutes of jitter. It uses
only the public official GitHub Release API; no GitHub token is needed. This
systemd installation manages one updater per host. Additional validators can use
separate private directories and independently scheduled `tick` commands.

## Update behavior

- Stable versions only; no downgrades or automatic major-version migrations.
- Verify official HTTPS origins, release-asset hashes, `SHA256SUMS`, supported
  policies, the immutable GHCR digest and the image's reported version.
- Pull before stopping; defer if the journal has an uncertain submission.
- Preserve the reviewed startup command, wallet mounts, signing flag and volumes.
- Stop the old process gracefully, run the candidate's offline doctor against
  existing state, then start the candidate and require a fresh healthy result.
- Never run two signing processes, reset journals, restore old state, refresh
  authorization, download replacement updater code or register a wallet.

Checksums authenticate consistency with the official release over HTTPS; this
workflow does not claim a separate offline release signature. Control of the
official GitHub repository is part of the trust model.

## Status, pause and recovery

```bash
python3 /opt/everyframe-validator-updater/auto-update.py status --root /opt/everyframe-validator-updater
python3 /opt/everyframe-validator-updater/auto-update.py disable --root /opt/everyframe-validator-updater
python3 /opt/everyframe-validator-updater/auto-update.py enable --root /opt/everyframe-validator-updater --yes
journalctl -u everyframe-validator-update.service
```

A failed update retains `pending.json` and the newest validator state. If startup
fails, the candidate is stopped; it does **not** automatically downgrade a signing
validator after the new code might have accepted a new policy. A Docker timeout
leaves the marker and requires checking the daemon's actual state before another
mutation. Repeated timer calls refuse to proceed while the marker exists.

Inspect the recorded old/new image, container, health and journal. Reconcile any
uncertain transaction and select a compatible version before restarting. Do not
delete validator state or clear the pending marker until the actual deployment has
been reconciled. Once the intended image is healthy, correct `current.json` and
archive `pending.json` as an operator-reviewed recovery. Installing new code does
not renew expired signing authorization or opt into a newly announced pool.
