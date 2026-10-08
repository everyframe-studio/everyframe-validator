import base64
import json
from copy import deepcopy
from unittest.mock import patch

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from everyframe_validator.core.chain_scope import scope
from everyframe_validator.feed import verify, strict_json, fetch, validate_url, validate_payload, MAX_BYTES
from everyframe_validator.publish import sign, project
from conftest import artifact, hot


def test_signed_round_trip_and_privacy(payload, signing):
    raw = json.dumps(sign(payload, signing[0])).encode()
    assert verify(raw, signing[1], scope("test"), 10) == payload
    assert b"PRIVATE" not in raw
    assert "proofs" not in payload["artifact"]["window"]
    assert "not_scored" not in payload["bindings"]


def test_tamper_and_wrong_signer_fail(payload, signing):
    e = sign(payload, signing[0])
    e["payload"]["bindings"]["a"] = hot(4)
    with pytest.raises(InvalidSignature):
        verify(json.dumps(e), signing[1], scope("test"), 10)
    with pytest.raises(InvalidSignature):
        verify(json.dumps(sign(payload, Ed25519PrivateKey.generate())), signing[1], scope("test"), 10)


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":1.5}', '{"x":Infinity}', 'x' * (MAX_BYTES+1)])
def test_strict_json(raw):
    with pytest.raises(ValueError):
        strict_json(raw)


@pytest.mark.parametrize("domain,epoch", [(scope("finney"),10),(scope("test"),9),(scope("test"),11),(scope("test"),True)])
def test_wrong_scope_and_stale_epoch(payload, signing, domain, epoch):
    with pytest.raises(ValueError):
        verify(json.dumps(sign(payload, signing[0])), signing[1], domain, epoch)


@pytest.mark.parametrize("change", ["negative", "bool", "duplicate", "extra", "bad_key", "shared_key", "missing_binding", "zero_jobs", "bad_price", "private_record"])
def test_invalid_payload(payload, change):
    a = payload["artifact"]
    row = a["window"]["scores"][0]
    if change == "negative": row["earningsMicrousd"] = -1
    if change == "bool": row["earningsMicrousd"] = True
    if change == "duplicate": a["window"]["scores"].append(row.copy())
    if change == "extra": payload["url"] = "http://localhost/"
    if change == "bad_key": payload["bindings"]["a"] = "bad"
    if change == "shared_key": payload["bindings"]["a"] = payload["bindings"]["b"]
    if change == "missing_binding": del payload["bindings"]["a"]
    if change == "zero_jobs": row["jobs"] = 0
    if change == "bad_price": a["alphaPriceUsd"] = "0"
    if change == "private_record": a["window"]["proofs"] = []
    with pytest.raises((ValueError, KeyError)):
        validate_payload(payload, scope("test"), 10)


def test_mainnet_projection_has_chain_derived_share():
    a = artifact("finney")
    p = project(a, {"a": hot(2), "b": hot(3)})
    validate_payload(p, scope("finney"), 10)
    p["artifact"]["minerShare"] = "0.41"
    with pytest.raises(ValueError):
        validate_payload(p, scope("finney"), 10)


@pytest.mark.parametrize("url", ["http://example.com/", "https://a:b@example.com/", "https://example.com:444/", "https://example.com/?key=x", "https://example.com/#x", "https://example.com/not-directory"])
def test_unsafe_feed_urls(url):
    with pytest.raises(ValueError): validate_url(url)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"])
def test_private_dns_not_contacted(ip):
    with patch("socket.getaddrinfo", return_value=[(0,0,0,"",(ip,443))]), patch("socket.create_connection") as connect:
        with pytest.raises(ValueError): fetch("https://example.com/epochs/", 10)
        connect.assert_not_called()
