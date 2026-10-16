"""Recovery must never release a lease based only on a successful restart."""

import fcntl
import json
import os
from unittest.mock import Mock

import pytest

from openjarvis.workflow import providers
from openjarvis.workflow.gpu_recovery import LOCAL_WORKER, GPURecovery


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    lock = tmp_path / "gpu.lock"
    lock.touch()
    monkeypatch.setenv("OPENJARVIS_GPU_LOCK_PATH", str(lock))
    providers.block_gpu(LOCAL_WORKER, {0: (16 * 1024**3, 0)})
    monitor = GPURecovery()
    monitor.stop = Mock(is_set=Mock(return_value=False), wait=Mock(return_value=False))
    marker = tmp_path / "gpu.lock.blocked"
    monkeypatch.setattr(
        providers,
        "request",
        Mock(return_value={"models": [], "queue_running": [], "queue_pending": []}),
    )
    monkeypatch.setattr(
        providers,
        "cleanup_snapshot",
        Mock(return_value=(True, {"running": 0, "pending": 0})),
    )
    restart = Mock()
    monkeypatch.setattr("openjarvis.workflow.gpu_recovery.subprocess.run", restart)
    return monitor, lock, marker, restart


def test_delayed_cleanup_recovers_without_restart(recovery):
    monitor, _, marker, restart = recovery
    monitor.tick()
    assert not marker.exists()
    restart.assert_not_called()


def test_active_lease_is_untouched(recovery):
    monitor, lock, marker, restart = recovery
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        monitor.tick()
        assert marker.exists()
        providers.request.assert_not_called()
        restart.assert_not_called()
    finally:
        os.close(fd)


def test_queued_work_is_never_interrupted(recovery):
    monitor, _, marker, restart = recovery
    providers.cleanup_snapshot.return_value = (False, {"running": 1, "pending": 0})
    for _ in range(5):
        monitor.tick()
    assert marker.exists()
    restart.assert_not_called()
    assert not any(call.args[1] == "/free" for call in providers.request.call_args_list)


def test_restart_requires_repeated_failure_and_retains_marker(recovery):
    monitor, _, marker, restart = recovery
    providers.cleanup_snapshot.side_effect = RuntimeError("HIP failure")
    monitor.tick()
    monitor.tick()
    restart.assert_not_called()
    monitor.tick()
    restart.assert_called_once()
    assert marker.exists()
    assert json.loads(marker.read_text())["restarts"] == 1
    monitor.tick()  # persistent cooldown prevents restart storms
    assert restart.call_count == 1


def test_restart_budget_survives_monitor_recreation(recovery):
    monitor, _, marker, restart = recovery
    state = json.loads(marker.read_text())
    state["restarts"] = 3
    marker.write_text(json.dumps(state))
    providers.cleanup_snapshot.side_effect = RuntimeError("HIP failure")
    for _ in range(4):
        monitor.tick()
    assert marker.exists()
    restart.assert_not_called()


@pytest.mark.parametrize(
    "contents",
    [
        "legacy marker",
        '{"owner":"another-worker"}',
        '{"owner":"openjarvis-image-v1","url":"http://other-host:8188"}',
    ],
)
def test_unknown_markers_are_not_acted_on(recovery, contents):
    monitor, _, marker, restart = recovery
    marker.write_text(contents)
    monitor.tick()
    assert marker.read_text() == contents
    providers.request.assert_not_called()
    restart.assert_not_called()


def test_two_healthy_samples_required(recovery):
    monitor, _, marker, restart = recovery
    providers.cleanup_snapshot.side_effect = [
        (True, {"running": 0, "pending": 0}),
        (False, {"running": 0, "pending": 0}),
    ]
    monitor.tick()
    assert marker.exists()
    restart.assert_not_called()


def test_ollama_must_unload_before_release(recovery):
    monitor, _, marker, restart = recovery
    providers.request.return_value = {"models": [{"name": "chat-model"}]}
    monitor.tick()
    assert marker.exists()
    providers.cleanup_snapshot.assert_not_called()
    restart.assert_not_called()


def test_shutdown_does_not_release_marker(recovery):
    monitor, _, marker, restart = recovery
    monitor.stop.wait.return_value = True
    monitor.tick()
    assert marker.exists()
    restart.assert_not_called()


def test_failed_restart_preserves_budget_and_block(recovery):
    import subprocess

    monitor, _, marker, restart = recovery
    providers.cleanup_snapshot.side_effect = RuntimeError("HIP failure")
    restart.side_effect = subprocess.TimeoutExpired("systemctl", 45)
    monitor.tick()
    monitor.tick()
    with pytest.raises(subprocess.TimeoutExpired):
        monitor.tick()
    assert marker.exists()
    assert json.loads(marker.read_text())["restarts"] == 1


def test_ollama_failure_does_not_restart_comfyui(recovery):
    monitor, _, marker, restart = recovery
    providers.request.side_effect = RuntimeError("Ollama unavailable")
    for _ in range(4):
        monitor.tick()
    assert marker.exists()
    restart.assert_not_called()


def test_busy_queue_with_broken_gpu_stats_is_not_restarted(recovery):
    monitor, _, marker, restart = recovery
    providers.request.return_value = {
        "models": [],
        "queue_running": [[1, "active"]],
        "queue_pending": [],
    }
    providers.cleanup_snapshot.side_effect = RuntimeError("HIP failure")
    for _ in range(4):
        monitor.tick()
    providers.cleanup_snapshot.assert_not_called()
    restart.assert_not_called()
    assert marker.exists()


def test_restarted_worker_must_pass_health_checks(recovery):
    monitor, _, marker, restart = recovery
    providers.cleanup_snapshot.side_effect = RuntimeError("HIP failure")
    for _ in range(3):
        monitor.tick()
    assert marker.exists()
    state = json.loads(marker.read_text())
    state["last_restart"] = 0
    marker.write_text(json.dumps(state))
    providers.cleanup_snapshot.side_effect = None
    monitor.tick()
    assert not marker.exists()
    restart.assert_called_once()
