"""Offline failure tests: no Docker, credentials or chain writes."""
import importlib.util
import json
import subprocess
from pathlib import Path
import pytest
from everyframe_validator.healthcheck import ready

spec = importlib.util.spec_from_file_location("deploy_release", Path(__file__).resolve().parents[1] / "deploy/deploy-release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
OLD = "ghcr.io/everyframe-studios/everyframe-validator@sha256:" + "a" * 64
NEW = "ghcr.io/everyframe-studios/everyframe-validator@sha256:" + "b" * 64


@pytest.mark.parametrize("state", ["dry_run", "already_submitted", "pending_reveal", "awaiting_readback", "finalized"])
def test_ready_states(state):
    assert ready({"state": state, "atMs": 999_000}, now=1000, started_at=900)


@pytest.mark.parametrize("status", [None, {}, {"state": "dry_run", "atMs": "999000"}, {"state": "failed", "atMs": 999_000}, {"state": "blocked", "atMs": 999_000}, {"state": "waiting", "atMs": 999_000}, {"state": "dry_run", "atMs": 899_000}, {"state": "dry_run", "atMs": 1_006_000}])
def test_bad_or_previous_container_status_not_ready(status):
    assert not ready(status, now=1000, started_at=900)


def test_stale_status():
    assert not ready({"state": "dry_run", "atMs": 500_000}, now=1000, started_at=1)


@pytest.mark.parametrize("image", ["latest", OLD + ";echo bad", OLD + "\n", OLD.replace("everyframe-studios", "attacker"), OLD.replace("sha256:", "sha512:")])
def test_reject_mutable_or_untrusted_image(image):
    with pytest.raises(ValueError):
        release.valid_image(image)


def test_success_preserves_state_and_stops_before_start(tmp_path):
    (tmp_path / "current-image.txt").write_text(OLD)
    (tmp_path / "journal.db").write_bytes(b"do not restore or delete me")
    calls = []
    result = release.deploy(tmp_path, NEW, lambda *args: calls.append(args))
    assert [a[2] for a in calls] == ["pull", "stop", "up"]
    assert calls[1][1] == OLD
    assert result["signingEnabled"] is False
    assert (tmp_path / "current-image.txt").read_text().strip() == NEW
    assert not (tmp_path / "deployment-pending.json").exists()
    assert (tmp_path / "journal.db").read_bytes() == b"do not restore or delete me"


def test_pull_failure_leaves_running_validator(tmp_path):
    (tmp_path / "current-image.txt").write_text(OLD)
    calls = []
    def call(*args):
        calls.append(args)
        raise RuntimeError("pull failed")
    with pytest.raises(RuntimeError):
        release.deploy(tmp_path, NEW, call)
    assert len(calls) == 1 and calls[0][2] == "pull"
    assert (tmp_path / "current-image.txt").read_text() == OLD
    assert not (tmp_path / "deployment-pending.json").exists()


def test_failed_health_rolls_back_image_only(tmp_path):
    (tmp_path / "current-image.txt").write_text(OLD)
    calls = []
    def call(*args):
        calls.append(args)
        if args[1] == NEW and args[2] == "up":
            raise RuntimeError("unhealthy")
    with pytest.raises(RuntimeError, match="previous image restored"):
        release.deploy(tmp_path, NEW, call)
    assert [(a[1], a[2]) for a in calls] == [(NEW, "pull"), (OLD, "stop"), (NEW, "up"), (NEW, "stop"), (OLD, "up")]
    assert (tmp_path / "current-image.txt").read_text().strip() == OLD
    assert not (tmp_path / "deployment-pending.json").exists()


def test_first_failed_deployment_stays_stopped(tmp_path):
    calls = []
    def call(*args):
        calls.append(args)
        if args[2] == "up":
            raise RuntimeError("unhealthy")
    with pytest.raises(RuntimeError):
        release.deploy(tmp_path, NEW, call)
    assert [a[2] for a in calls] == ["pull", "stop", "up", "stop"]
    assert not (tmp_path / "current-image.txt").exists()


def test_failed_rollback_retains_marker(tmp_path):
    (tmp_path / "current-image.txt").write_text(OLD)
    def call(*args):
        if args[2] == "up":
            raise RuntimeError("unhealthy")
    with pytest.raises(RuntimeError):
        release.deploy(tmp_path, NEW, call)
    assert json.loads((tmp_path / "deployment-pending.json").read_text()) == {"previous": OLD, "requested": NEW}


def test_interrupted_deployment_refuses_blind_retry(tmp_path):
    (tmp_path / "deployment-pending.json").write_text("{}")
    def call(*args):
        pytest.fail("must not contact Docker")
    with pytest.raises(RuntimeError, match="Unfinished deployment"):
        release.deploy(tmp_path, NEW, call)


def test_timeout_preserves_marker_without_racing_daemon(tmp_path):
    (tmp_path / "current-image.txt").write_text(OLD)
    calls = []
    def call(*args):
        calls.append(args)
        if args[2] == "up":
            raise subprocess.TimeoutExpired("docker", 1200)
    with pytest.raises(RuntimeError, match="timed out"):
        release.deploy(tmp_path, NEW, call)
    assert [a[2] for a in calls] == ["pull", "stop", "up"]
    assert (tmp_path / "deployment-pending.json").exists()
