#!/usr/bin/env python3
"""Serialized image-only rollout. Never restore, delete or replace validator state."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

IMAGE = re.compile(r"ghcr\.io/everyframe-studios/everyframe-validator@sha256:[0-9a-f]{64}")
PROJECT = "everyframe-validator-canary"


def valid_image(value):
    if not IMAGE.fullmatch(value):
        raise ValueError("Use an immutable official image digest, not a tag.")
    return value


def atomic_write(path, text):
    fd, name = tempfile.mkstemp(prefix=".release-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def docker(root, image, *args):
    env = {**os.environ, "VALIDATOR_IMAGE": image}
    # Explicit file and project; never load an operator's implicit override file.
    subprocess.run(["docker", "compose", "--project-name", PROJECT,
                    "--file", str(root / "compose.yaml"), *args],
                   env=env, check=True, timeout=1200, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)


def deploy(root, image, call=docker):
    image = valid_image(image)
    pending = root / "deployment-pending.json"
    current = root / "current-image.txt"
    if pending.exists():
        raise RuntimeError("Unfinished deployment: inspect containers and deployment-pending.json before retrying.")
    previous = valid_image(current.read_text().strip()) if current.exists() else None
    # A failed pull leaves the existing container untouched.
    call(root, image, "pull", "validator")
    atomic_write(pending, json.dumps({"previous": previous, "requested": image}) + "\n")
    try:
        # Never run old and new workers concurrently against the same state.
        call(root, previous or image, "stop", "validator")
        call(root, image, "up", "-d", "--no-build", "--pull", "never",
             "--wait", "--wait-timeout", "480", "validator")
        atomic_write(current, image + "\n")
        pending.unlink()
    except subprocess.TimeoutExpired:
        # The daemon may still be handling an interrupted client request.
        # Do not race it with another mutation; preserve the reconciliation marker.
        raise RuntimeError("Docker operation timed out; inspect the deployment before retrying.") from None
    except Exception:
        # Image-only rollback: state and journals remain at their newest version.
        # This canary has no wallets/--submit. Production signing rollbacks need
        # separate review of journal/schema compatibility and pending commits.
        call(root, image, "stop", "validator")
        if previous:
            call(root, previous, "up", "-d", "--no-build", "--pull", "never",
                 "--wait", "--wait-timeout", "480", "validator")
            atomic_write(current, previous + "\n")
        pending.unlink()
        raise RuntimeError("Readiness failed; previous image restored, or first deployment stopped. State preserved.") from None
    return {"deployed": image, "previous": previous, "signingEnabled": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=valid_image)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    os.umask(0o077)
    try:
        with (root / "deployment.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print(json.dumps(deploy(root, args.image)))
    except Exception:
        print("Deployment failed. Inspect the host's deployment-pending.json and Docker health locally; do not delete validator state.", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
