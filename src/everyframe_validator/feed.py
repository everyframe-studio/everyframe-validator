"""Bounded HTTPS download, pinned accounting signer, privacy-safe wire schema."""
import base64
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from urllib.parse import urlsplit

import certifi
from cryptography.hazmat.primitives.serialization import load_der_public_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .core.chain_scope import check_record
from .core.reward_policy import canonical, validate_artifact

SCHEMA = "everyframe.validator.epoch.v1"
MAX_BYTES = 2_000_000
PROFILES = {
    "mainnet": {
        "network": "finney", "netuid": 117, "versionKey": 1030,
        "feed": "https://subnet.everyframe.studio/mainnet/v1/validator/epochs/",
        "publicKey": "MCowBQYDK2VwAyEAkgU6E2ap4bOhyjJ4r/f5pjdE4abHpEAjJq0q2S/zan4=",
    },
    "testnet": {
        "network": "test", "netuid": 566, "versionKey": 2,
        "feed": "https://subnet.everyframe.studio/v1/validator/epochs/",
        "publicKey": "MCowBQYDK2VwAyEAe8///9EC+z/zha4IdGNQYDvXV12MA2Z3aWe0oLAy4fc=",
    },
}
ARTIFACT_KEYS = set("version policy network netuid genesis epoch blocksPerEpoch startBlock endBlock finalizedBlock endBlockHash closeBlockHash emissionsAlpha alphaPriceUsd minerShare priceSource window".split())
MAINNET_KEYS = set("ownerCutU16 ownerCutEnabled unusedAllocation accountingWindow".split())
WINDOW_KEYS = set("version policy start end finalizedAt scores".split())
SCORE_KEYS = set("minerId earningsMicrousd surchargeMicrousd jobs".split())
PRICE_KEYS = set("source observedAt taoUsd candleHash".split())


class FeedUnavailable(ValueError):
    """Newest epoch is not published; do not substitute an older epoch or zero."""


def strict_json(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError("document_too_large")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def invalid(_):
        raise ValueError("invalid_json_number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid, parse_float=invalid)


def public_key(encoded):
    key = load_der_public_key(base64.b64decode(encoded, validate=True))
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("ed25519_key_required")
    return key


def exact_keys(value, keys):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid_document_fields")


def validate_payload(payload, domain, epoch):
    exact_keys(payload, {"schema", "artifact", "bindings", "sourceHash"})
    if payload["schema"] != SCHEMA or not re.fullmatch(r"[a-f0-9]{64}", payload["sourceHash"]):
        raise ValueError("invalid_artifact_schema")
    a = payload["artifact"]
    exact_keys(a, ARTIFACT_KEYS | (MAINNET_KEYS if domain["network"] == "finney" else set()))
    check_record(a, domain)
    if type(epoch) is not int or a["epoch"] != epoch:
        raise ValueError("wrong_epoch")
    for k in ("genesis", "endBlockHash", "closeBlockHash"):
        if not re.fullmatch(r"0x[a-f0-9]{64}", a[k]):
            raise ValueError("invalid_chain_hash")
    w = a["window"]
    exact_keys(w, WINDOW_KEYS)
    if any(type(w[k]) is not int or not 0 <= w[k] < 2**63 for k in ("start", "end", "finalizedAt")):
        raise ValueError("invalid_timestamp")
    p = a["priceSource"]
    if not isinstance(p, dict) or set(p) - PRICE_KEYS or not {"source", "observedAt"} <= set(p):
        raise ValueError("invalid_price_fields")
    if not isinstance(p["source"], str) or not 1 <= len(p["source"]) <= 100 or type(p["observedAt"]) is not int:
        raise ValueError("invalid_price_source")
    if "candleHash" in p and not re.fullmatch(r"[a-f0-9]{64}", p["candleHash"]):
        raise ValueError("invalid_price_hash")
    for key in ("alphaPriceUsd", "emissionsAlpha", "minerShare"):
        if not isinstance(a[key], str) or len(a[key]) > 120:
            raise ValueError("invalid_economics")
    if not isinstance(w["scores"], list) or len(w["scores"]) > 4096:
        raise ValueError("invalid_scores")
    for row in w["scores"]:
        exact_keys(row, SCORE_KEYS)
        if not isinstance(row["minerId"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", row["minerId"]):
            raise ValueError("invalid_miner_id")
        if any(type(row[k]) is not int or not 0 <= row[k] < 2**63 for k in ("earningsMicrousd", "surchargeMicrousd", "jobs")):
            raise ValueError("invalid_score")
        if row["jobs"] == 0 and row["earningsMicrousd"] + row["surchargeMicrousd"]:
            raise ValueError("earnings_without_work")
    bindings = payload["bindings"]
    if not isinstance(bindings, dict) or set(bindings) != {r["minerId"] for r in w["scores"]}:
        raise ValueError("bindings_must_match_scored_miners")
    from bittensor.wallets import is_bittensor_address
    for hotkey in bindings.values():
        if not isinstance(hotkey, str) or not is_bittensor_address(hotkey):
            raise ValueError("invalid_hotkey")
    if len(set(bindings.values())) != len(bindings):
        raise ValueError("duplicate_hotkey_binding")
    validate_artifact(a)
    return payload


def verify(raw, key, domain, epoch):
    envelope = strict_json(raw)
    exact_keys(envelope, {"payload", "signature"})
    signature = base64.b64decode(envelope["signature"], validate=True)
    public_key(key).verify(signature, canonical(envelope["payload"]).encode())
    return validate_payload(envelope["payload"], domain, epoch)


def validate_url(base):
    u = urlsplit(base)
    if (u.scheme != "https" or not u.hostname or u.port not in (None, 443)
            or u.username or u.password or u.query or u.fragment or not u.path.endswith("/")
            or not base.isascii() or any(ord(c) <= 32 for c in base)):
        raise ValueError("feed_requires_https_directory_url")
    return u


def fetch(base, epoch):
    """No proxies, redirects, arbitrary ports, private IPs, or DNS rebinding."""
    u = validate_url(base)
    if type(epoch) is not int or epoch < 0:
        raise ValueError("invalid_epoch")
    addresses = socket.getaddrinfo(u.hostname, 443, type=socket.SOCK_STREAM)
    ips = list(dict.fromkeys(row[4][0] for row in addresses))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError("feed_must_be_public")
    context = ssl.create_default_context(cafile=certifi.where())
    context.keylog_filename = None
    conn = http.client.HTTPSConnection(u.hostname, timeout=10, context=context)
    try:
        # Preserve hostname verification/SNI while connecting to a validated IP.
        sock = socket.create_connection((ips[0], 443), timeout=10)
        try:
            conn.sock = context.wrap_socket(sock, server_hostname=u.hostname)
        except BaseException:
            sock.close()
            raise
        conn.request("GET", u.path + str(epoch) + ".json", headers={"Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "everyframe-validator/0.1.0"})
        response = conn.getresponse()
        if response.status == 404:
            raise FeedUnavailable("newest_epoch_not_published")
        if response.status != 200 or response.getheader("Content-Encoding", "identity") != "identity":
            raise ValueError("feed_http_error")
        if int(response.getheader("Content-Length", "0")) > MAX_BYTES:
            raise ValueError("document_too_large")
        pieces, length, deadline = [], 0, time.monotonic() + 20
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("feed_deadline")
            part = response.read1(min(65536, MAX_BYTES + 1 - length))
            if not part:
                break
            pieces.append(part)
            length += len(part)
            if length > MAX_BYTES:
                raise ValueError("document_too_large")
        return b"".join(pieces)
    finally:
        conn.close()
