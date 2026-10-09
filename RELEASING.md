# Releasing EveryFrame Validator

Releases provide Python wheel/source distributions and a Linux amd64 image at
`ghcr.io/everyframe-studios/everyframe-validator`. Publishing does not deploy,
update third-party validators, or authorize signing. PyPI is not configured.

## One-time GitHub setup

1. Enable Actions, protect `main` with CI/PR review, and restrict creation,
   updates and deletion of `v*` tags to release maintainers.
2. Create the `release` environment: require a reviewer, prevent self-review,
   disallow admin bypass, and allow version tags plus `main` for manual retries.
   These are GitHub settings; YAML cannot enable environment protection.
3. Allow `GITHUB_TOKEN` to publish repository packages/releases. No PAT needed.
4. After the first image push, open organization Packages → everyframe-validator
   → Package settings → change visibility to **Public**. GHCR defaults new
   packages to private. The workflow refuses to publish a GitHub Release until
   anonymous image access succeeds. Set visibility and rerun the failed workflow;
   it reuses the image only if its revision and version match.
5. For optional deployment, configure [the host and environments](deploy/README.md).

## Publish a version

1. Make a branch and PR. Set the same version in `pyproject.toml` and
   `src/everyframe_validator/__init__.py`. Both already specify `0.1.0` for the
   first release. Review sources, dependency licenses and package contents.
2. Merge after CI passes. From the reviewed main commit:

   ```bash
   git switch main
   git pull --ff-only
   git tag -a v0.1.0 -m "EveryFrame Validator v0.1.0"
   git push origin v0.1.0
   ```

3. Approve **Release validator**. It checks version/tag/main ancestry, tests and
   clean wheel installs on Python 3.10, 3.13 and 3.14, builds a Linux amd64 image,
   smoke-tests the CLI without networking, verifies public image access, and
   publishes the Release assets. It does not invoke a chain transaction.
4. Download assets and run `sha256sum --check SHA256SUMS`. Assets include wheel,
   source archive, deployment tools, and the immutable digest in `image.txt`.

No moving `latest` tag is published. Published releases cannot be overwritten
by this workflow. Never move release tags. Retry an unpublished release using
**Run workflow → tag** or rerun failed jobs; a source change requires a new
version. Interrupted draft releases are not deployable.

## Deploy separately

Dispatch **Deploy validator canary** from `main`, select a published tag and
environment, and obtain reviewer approval. It verifies the release digest and
checksums before using pinned SSH. GitHub records the deployment result.

This deploys a separate **dry-run canary**, not an upgrade of a signing validator.
Readiness requires a fresh successful accounting iteration after startup, not
merely a running process. A missing epoch/feed is not ready. Before signing,
verify the feed and end-to-end canary, review schema compatibility and pending
submissions, and explicitly authorize the intended wallet. Never restore an
old journal to roll back code.

References: [GHCR visibility](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry),
[environment protection](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments).
