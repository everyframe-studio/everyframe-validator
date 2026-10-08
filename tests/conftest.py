import base64
import hashlib
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bittensor.keyfiles import Keypair
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from everyframe_validator.core.chain_scope import scope
from everyframe_validator.core.reward_policy import POLICY
from everyframe_validator.feed import PROFILES
from everyframe_validator.publish import project, sign
from everyframe_validator.state import initialize, atomic_json


def hot(n):
    return Keypair(public_key=bytes([n]) * 32, ss58_format=42).ss58_address


def artifact(network="test", scores=None):
    a = {"version": 2, "policy": POLICY, **scope(network), "epoch": 10,
         "blocksPerEpoch": 361, "startBlock": 3610, "endBlock": 3971, "finalizedBlock": 3975,
         "endBlockHash": "0x" + "a"*64, "closeBlockHash": "0x" + "b"*64,
         "emissionsAlpha": "1000", "alphaPriceUsd": "10/41", "minerShare": "0.41",
         "priceSource": {"source": "TEST FIXTURE", "observedAt": 960000},
         "window": {"version": 2, "policy": POLICY, "start": 100000, "end": 1000000,
                    "finalizedAt": 1000100, "scores": scores if scores is not None else [
                        {"minerId": "a", "earningsMicrousd": 30000000, "surchargeMicrousd": 0, "jobs": 2},
                        {"minerId": "b", "earningsMicrousd": 20000000, "surchargeMicrousd": 0, "jobs": 1}],
                    "proofs": [{"jobId": "PRIVATE_JOB", "prompt": "PRIVATE_PROMPT"}], "excluded": []}}
    if network == "finney":
        a.update(ownerCutU16=11796, ownerCutEnabled=True, minerShare=str(Fraction(53739,131070)),
                 unusedAllocation="Recycle", accountingWindow="fixed-tempo-plus-one-v1")
    return a


@pytest.fixture
def signing():
    key = Ed25519PrivateKey.generate()
    encoded = base64.b64encode(key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).decode()
    return key, encoded


@pytest.fixture
def payload():
    return project(artifact(), {"a": hot(2), "b": hot(3), "not_scored": hot(9)})


@pytest.fixture
def profile(tmp_path, signing):
    c = {**PROFILES["testnet"], "publicKey": signing[1], "validatorHotkey": hot(1),
         "wallet": "test-wallet", "walletHotkey": "default", "walletPath": None, "walletPasswordFile": None}
    root = tmp_path / "state"
    initialize(root, c)
    return root, c


@pytest.fixture
def chain(monkeypatch):
    import bittensor as bt
    neurons = [SimpleNamespace(uid=i, hotkey=hot(i), coldkey=hot(i+10), validator_permit=i in (0,1)) for i in range(4)]
    params = {"tempo": 360, "weights_version": 2, "commit_reveal_weights_enabled": True,
              "weights_rate_limit": 100, "min_allowed_weights": 1, "max_weights_limit": 65535}
    graph = SimpleNamespace(block=4000, neurons=neurons)

    network = {"name": "test"}

    async def block_hash(block):
        if block == 0:
            return scope(network["name"])["genesis"]
        return "0x" + ("b" if block == 3970 else "a") * 64

    async def query(storage, args, block=None):
        if storage[1] == "SubnetOwner":
            return hot(10)
        if storage[1] == "SubnetOwnerHotkey":
            return hot(0)
        if storage[1] == "LastUpdate":
            return [0] * 4
        raise ValueError("unexpected test query")

    class Client:
        def __init__(self, **kwargs):
            self._substrate = SimpleNamespace(block_hash=block_hash, raw=SimpleNamespace(
                get_chain_finalised_head=AsyncMock(return_value="finalized"), get_block_number=AsyncMock(return_value=4000)),
                account_next_index=AsyncMock(return_value=1),
                sign_extrinsic=AsyncMock(return_value=(b"fixture transaction", "0x" + hashlib.blake2b(b"fixture transaction", digest_size=32).hexdigest())),
                submit_signed=execute, find_extrinsic=AsyncMock(return_value=None))
            self.subnets = SimpleNamespace(metagraph=AsyncMock(return_value=graph), subnet_hyperparameters=AsyncMock(return_value=params))
            self.query = query
            self.execute = execute
            self.plan = preview

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    result = {"success": True, "block_hash": "0x"+"c"*64, "extrinsic_id": "4000-0"}
    execute = AsyncMock(return_value=SimpleNamespace(**result, to_dict=lambda: result))
    preview = AsyncMock(return_value=SimpleNamespace(ok=True, call="FAKE_POLICY_CHECKED_CALL"))
    wallet = SimpleNamespace(hotkeypub=SimpleNamespace(ss58_address=hot(1)), get_hotkey=lambda _: SimpleNamespace(ss58_address=hot(1)))
    monkeypatch.setattr(bt, "Client", Client)
    monkeypatch.setattr(bt, "Wallet", lambda *args, **kwargs: wallet)
    return SimpleNamespace(execute=execute, preview=preview, params=params, neurons=neurons, graph=graph, wallet=wallet, network=network)
