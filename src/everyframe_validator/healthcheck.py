"""Read-only container readiness; never unlock a key or submit a transaction."""
import json
import os
from pathlib import Path
import sys
import time

from .state import load_config, private_directory, read_private


def ready(status, *, now, started_at, max_age=420):
    if not isinstance(status, dict) or type(status.get("atMs")) is not int:
        return False
    at = status["atMs"] / 1000
    # Old status files must not make a newly started, broken image look healthy.
    return (
        started_at <= at <= now + 5
        and now - at <= max_age
        and status.get("state") in {
            "dry_run", "already_submitted", "pending_reveal", "awaiting_readback",
            "finalized",
        }
    )


def container_started_at():
    # /proc/1 is the validator entrypoint, not this short-lived health process.
    fields = Path("/proc/1/stat").read_text().rsplit(")", 1)[1].split()
    start_ticks = int(fields[19])  # proc stat field 22, after pid and comm
    uptime = float(Path("/proc/uptime").read_text().split()[0])
    return time.time() - uptime + start_ticks / os.sysconf("SC_CLK_TCK")


def main():
    try:
        root = private_directory(Path(sys.argv[1] if len(sys.argv) > 1 else "/state"))
        load_config(root)
        ok = ready(read_private(root / "status.json"), now=time.time(),
                   started_at=container_started_at())
    except Exception:
        ok = False
    # No raw profile, provider response, wallet path, or exception in Docker health logs.
    print(json.dumps({"ready": ok}))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
