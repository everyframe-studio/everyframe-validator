import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption

from everyframe_validator.core.chain_scope import scope, check_authorization
from everyframe_validator.core.reward_policy import seal_epoch, POLICY
from everyframe_validator.core.validator import reserve_run
from everyframe_validator.feed import verify
from everyframe_validator.publish import publish
from everyframe_validator.state import load_config, atomic_json, initialize, read_private
from conftest import artifact, hot


def test_publisher_sealed_db_round_trip_and_immutability(tmp_path,signing):
    db=tmp_path/"sealed.db"
    a=artifact()
    seal_epoch(db,a)
    bindings=tmp_path/"bindings.json"
    bindings.write_text(json.dumps({"a":hot(2),"b":hot(3)}))
    key=tmp_path/"key.pem"
    key.write_bytes(signing[0].private_bytes(Encoding.PEM,PrivateFormat.PKCS8,NoEncryption()))
    key.chmod(0o600)
    args=SimpleNamespace(network="testnet",epochs=str(db),epoch=10,bindings=str(bindings),
        signing_key=str(key),output=str(tmp_path/"public"),expected_public_key=signing[1])
    result=publish(args)
    raw=Path(result["publicFile"]).read_bytes()
    verified=verify(raw,signing[1],scope("test"),10)
    assert verified["artifact"]["window"]["scores"]==a["window"]["scores"]
    assert b"PRIVATE" not in raw
    assert Path(result["publicFile"]).stat().st_mode & 0o777 == 0o644
    assert publish(args)==result
    bindings.write_text(json.dumps({"a":hot(4),"b":hot(3)}))
    with pytest.raises(ValueError,match="immutable_file_conflict"):publish(args)
    assert Path(result["publicFile"]).read_bytes()==raw
    key.chmod(0o644)
    with pytest.raises(ValueError,match="owner_only"):publish(args)


@pytest.mark.parametrize("kind",["missing_journal","symlink_journal","world_readable","other_network"])
def test_unsafe_state_fails(profile,tmp_path,kind):
    root,c=profile
    path=root/"journal.db"
    if kind=="missing_journal":path.unlink()
    if kind=="symlink_journal":
        moved=tmp_path/"moved.db";path.rename(moved);path.symlink_to(moved)
    if kind=="world_readable":path.chmod(0o644)
    if kind=="other_network":
        with sqlite3.connect(path) as db:db.execute("UPDATE chain_scope SET netuid=117")
    with pytest.raises((ValueError,sqlite3.Error)):load_config(root)


def test_private_json_rejects_symlink_and_fifo(profile,tmp_path):
    root,c=profile
    link=tmp_path/"link"
    link.symlink_to(root/"config.json")
    with pytest.raises(OSError):read_private(link)
    fifo=tmp_path/"fifo"
    os.mkfifo(fifo,mode=0o600)
    with pytest.raises(ValueError):read_private(fifo)


def test_local_consent_expiry_scope_and_budget(profile):
    root,c=profile
    consent={**scope("test"),"action":"submit-reward-weights","validatorHotkey":hot(1),
        "versionKey":2,"expiresAt":2000,"maxSubmissions":1,"id":"one"}
    path=root/"authorization.json"
    atomic_json(path,consent)
    assert check_authorization(path,scope("test"),hot(1),2,now=1000)==consent
    with pytest.raises(ValueError):check_authorization(path,scope("test"),hot(1),2,now=2000)
    with pytest.raises(ValueError):check_authorization(path,scope("finney"),hot(1),2,now=1000)
    with pytest.raises(ValueError):check_authorization(path,scope("test"),hot(2),2,now=1000)
    record={**scope("test"),"authorizationId":"one"}
    with sqlite3.connect(root/"journal.db") as db:
        reserve_run(db,"one",record,consent)
        db.execute("UPDATE weight_runs SET state='revealed'");db.commit()
        with pytest.raises(ValueError,match="budget exhausted"):reserve_run(db,"two",record,consent)


def test_persistent_epoch_dedup(profile):
    root,c=profile
    record={**scope("test"),"policy":POLICY,"validatorHotkey":hot(1),"epoch":10,"openEpoch":11}
    with sqlite3.connect(root/"journal.db") as db:
        reserve_run(db,"one",record)
        db.execute("UPDATE weight_runs SET state='revealed'");db.commit()
    with sqlite3.connect(root/"journal.db") as db:
        with pytest.raises(ValueError,match="epoch already submitted"):reserve_run(db,"two",record)


def test_cli_init_mainnet_default_and_reinit_refusal(tmp_path):
    root=tmp_path/"mainnet"
    env={**os.environ,"PYTHONPATH":str(Path(__file__).parents[1]/"src")}
    cmd=[sys.executable,"-m","everyframe_validator","init","--state-dir",str(root),"--validator-hotkey",hot(1)]
    first=subprocess.run(cmd,env=env,capture_output=True,text=True)
    assert first.returncode==0,first.stdout
    assert json.loads(first.stdout)["netuid"]==117
    assert load_config(root)["network"]=="finney"
    assert not (root/"authorization.json").exists()
    second=subprocess.run(cmd,env=env,capture_output=True,text=True)
    assert second.returncode==2
