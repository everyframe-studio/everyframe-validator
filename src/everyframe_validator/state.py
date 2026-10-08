"""Private, chain/hotkey-bound state and exclusive local process lock."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

from .core.chain_scope import bind_database, check_database, scope
from .feed import strict_json


def private_directory(path, create=False):
    path = Path(path).absolute()
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("state_directory_symlink")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("state_directory_must_be_owned_and_mode_700")
    return path


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        s = os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077:
            raise ValueError("private_file_permissions")
        return strict_json(f.read(2_000_001))


def atomic_json(path, value, immutable=False):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, sort_keys=True, separators=(",", ":"), allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        if immutable:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.is_symlink() or strict_json(path.read_bytes()) != value:
                    raise ValueError("immutable_file_conflict")
        else:
            if path.is_symlink():
                raise ValueError("state_symlink")
            os.replace(temporary, path)
        fd_dir = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd_dir)
        finally:
            os.close(fd_dir)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked(path):
    root = private_directory(path)
    fd = os.open(root / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("invalid_lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield root
    finally:
        os.close(fd)


def initialize(root, config):
    root = private_directory(root, create=True)
    with locked(root):
        if any((root / name).exists() for name in ("config.json", "journal.db", "epochs.db")):
            raise ValueError("state_already_initialized")
        domain = scope(config["network"], config["netuid"])
        for name in ("journal.db", "epochs.db"):
            with sqlite3.connect(root / name) as db:
                bind_database(db, domain)
                if name == "journal.db":
                    db.execute("CREATE TABLE weight_runs(id TEXT PRIMARY KEY,state TEXT NOT NULL,record TEXT NOT NULL,result TEXT)")
                    db.execute("CREATE TABLE validator_identity(hotkey TEXT NOT NULL)")
                    db.execute("INSERT INTO validator_identity VALUES(?)", (config["validatorHotkey"],))
            (root / name).chmod(0o600)
        atomic_json(root / "config.json", config, immutable=True)
        atomic_json(root / "identity.json", {**domain, "hotkey": config["validatorHotkey"], "publicKey": config["publicKey"], "feed": config["feed"]}, immutable=True)


def load_config(root):
    c = read_private(root / "config.json")
    identity = read_private(root / "identity.json")
    domain = scope(c["network"], c["netuid"])
    if identity != {**domain, "hotkey": c["validatorHotkey"], "publicKey": c["publicKey"], "feed": c["feed"]}:
        raise ValueError("profile_identity_changed")
    for name in ("epochs.db", "journal.db"):
        p = root / name
        if p.is_symlink() or not p.is_file() or p.stat().st_mode & 0o077:
            raise ValueError("state_database_missing_or_unsafe")
        check_database(p, domain)
    with sqlite3.connect((root / "journal.db").as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("SELECT hotkey FROM validator_identity").fetchall() != [(c["validatorHotkey"],)]:
            raise ValueError("journal_validator_mismatch")
    return c


def journal_rows(root):
    with sqlite3.connect((root / "journal.db").as_uri() + "?mode=ro", uri=True) as db:
        return [{"id": row[0], "state": row[1], "record": json.loads(row[2]),
                 "result": json.loads(row[3]) if row[3] else {}}
                for row in db.execute("SELECT id,state,record,result FROM weight_runs")]


def status_snapshot(path):
    """Read committed SQLite state without taking the signing-operation lock.

    This is diagnostic only. Signing always revalidates under the exclusive lock.
    """
    import time
    root = private_directory(path)
    c = load_config(root)
    rows = journal_rows(root)
    busy = False
    try:
        fd = os.open(root / "lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        pass
    else:
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("invalid_lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                busy = True
        finally:
            os.close(fd)
    runs = []
    for row in rows:
        item = {"id": row["id"], "state": row["state"], "epoch": row["record"].get("epoch")}
        result = row["result"]
        if result.get("extrinsic_id"):
            item["extrinsicId"] = result["extrinsic_id"]
        broadcast = result.get("broadcast", {})
        if broadcast.get("hash"):
            item["transactionHash"] = broadcast["hash"]
        if row["state"] in ("unknown", "submitting"):
            item["next"] = ("Run reconcile to search for the exact finalized transaction."
                            if broadcast.get("hash") else
                            "No durable transaction hash is available. Preserve the journal for manual chain investigation; do not resubmit.")
        runs.append(item)
    snapshot = {"network": c["network"], "netuid": c["netuid"], "validatorHotkey": c["validatorHotkey"],
                "feed": c["feed"], "runs": runs, "diagnosticOnly": True, "operationInProgress": busy}
    try:
        last = read_private(root / "status.json")
    except FileNotFoundError:
        last = None
    snapshot["lastRun"] = last
    snapshot["lastRunAgeSeconds"] = (max(0, int(time.time() - last["atMs"] / 1000))
                                     if last and type(last.get("atMs")) is int else None)
    snapshot["state"] = ("blocked" if any(r["state"] in ("unknown", "submitting") for r in rows)
                         else "pending_reveal" if any(r["state"] == "pending_reveal" for r in rows)
                         else "awaiting_readback" if any(r["state"] == "finalized" for r in rows)
                         else "processing" if busy else "idle")
    return snapshot
