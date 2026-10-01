"""Durable schedules, bounded workers, recovery and cancellation without live APIs."""

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from openjarvis.connectors.local_files import LocalFilesConnector
from openjarvis.connectors.source_job_runner import SourceJobRunner
from openjarvis.connectors.source_manager import SourceManager
from openjarvis.connectors.source_store import SourceConflict, SourceStore
from openjarvis.connectors.store import KnowledgeStore


@pytest.fixture
def manager(tmp_path):
    manager = SourceManager(
        SourceStore(str(tmp_path / "sources.db"), legacy_path=str(tmp_path / "legacy")),
        knowledge_path=str(tmp_path / "knowledge.db"),
    )
    yield manager
    manager.stop_jobs()


def source(manager, tmp_path, name="Folder"):
    root = tmp_path / name
    root.mkdir()
    (root / "policy.txt").write_text("Migration approved")
    return manager.create("local_files", name, {"path": str(root)})


def wait_job(manager, identity):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = manager.jobs.get(identity)
        if job["state"] not in {"running", "queued"}:
            return job
        time.sleep(0.01)
    pytest.fail("Source job did not finish")


def test_manual_run_reports_final_progress_and_independent_history(manager, tmp_path):
    item = source(manager, tmp_path)
    job = manager.start_sync(item["id"])
    final = wait_job(manager, job["id"])
    assert final["state"] == "succeeded"
    assert final["documents_seen"] == final["chunks_written"] == 1
    assert final["started_at"] and final["finished_at"]
    assert manager.list()[0]["latest_job"]["id"] == job["id"]
    assert manager.jobs.history(item["id"])[0] == final


def test_schedule_persists_and_coalesces_missed_intervals(manager, tmp_path):
    item = source(manager, tmp_path)
    now = datetime.now(timezone.utc)
    schedule = manager.jobs.set_schedule(
        item["id"], 0, enabled=True, interval_seconds=300, now=now
    )
    assert manager.store.get(item["id"])["revision"] == 1
    restarted = SourceManager(
        SourceStore(str(manager.store.path), legacy_path=str(tmp_path / "legacy")),
        knowledge_path=manager.knowledge_path,
    )
    assert restarted.jobs.schedule(item["id"]) == schedule
    runner = SourceJobRunner(restarted)
    runner.tick(now + timedelta(seconds=299))
    assert restarted.jobs.history(item["id"]) == []
    later = now + timedelta(days=3)
    runner.tick(later)
    job = restarted.jobs.history(item["id"])[0]
    assert wait_job(restarted, job["id"])["state"] == "succeeded"
    runner.tick(later)
    assert len(restarted.jobs.history(item["id"])) == 1
    assert (
        restarted.jobs.schedule(item["id"])["next_run_at"]
        == (later + timedelta(seconds=300)).isoformat()
    )
    restarted.stop_jobs()


def test_schedule_updates_do_not_clear_index_and_reject_stale_edits(manager, tmp_path):
    item = source(manager, tmp_path)
    manager.sync(item["id"])
    manager.jobs.set_schedule(item["id"], 0, enabled=True, interval_seconds=300)
    with pytest.raises(SourceConflict, match="changed"):
        manager.jobs.set_schedule(item["id"], 0, enabled=False, interval_seconds=300)
    assert manager.list()[0]["chunks"] == 1
    assert (
        manager.jobs.set_schedule(item["id"], 1, enabled=False, interval_seconds=600)[
            "next_run_at"
        ]
        is None
    )


@pytest.mark.parametrize("interval", [True, 299, 604801, 300.5, "300"])
def test_schedule_rejects_invalid_intervals(manager, tmp_path, interval):
    item = source(manager, tmp_path)
    with pytest.raises(ValueError):
        manager.jobs.set_schedule(
            item["id"], 0, enabled=True, interval_seconds=interval
        )
    assert not manager.jobs.schedule(item["id"])["enabled"]


def test_disabled_source_pauses_schedule_without_losing_settings(manager, tmp_path):
    item = source(manager, tmp_path)
    now = datetime.now(timezone.utc)
    manager.jobs.set_schedule(
        item["id"], 0, enabled=True, interval_seconds=300, now=now
    )
    disabled = manager.update(
        item["id"], 1, name=item["name"], config=item["config"], enabled=False
    )
    SourceJobRunner(manager).tick(now + timedelta(seconds=301))
    assert manager.jobs.history(item["id"]) == []
    assert manager.jobs.schedule(item["id"])["enabled"]
    manager.update(
        item["id"],
        disabled["revision"],
        name=item["name"],
        config=item["config"],
        enabled=True,
    )
    SourceJobRunner(manager).tick(now + timedelta(seconds=301))
    assert (
        wait_job(manager, manager.jobs.history(item["id"])[0]["id"])["state"]
        == "succeeded"
    )


def test_queued_job_survives_restart_and_cannot_run_twice(manager, tmp_path):
    item = source(manager, tmp_path)
    job = manager.jobs.create(item)
    other = SourceManager(manager.store, knowledge_path=manager.knowledge_path)
    SourceJobRunner(other).tick()
    SourceJobRunner(manager).tick()
    assert wait_job(manager, job["id"])["state"] == "succeeded"
    assert len(manager.jobs.history(item["id"])) == 1
    other.stop_jobs()


def test_queued_cancel_and_configuration_change_never_fetch(manager, tmp_path):
    item = source(manager, tmp_path)
    queued = manager.jobs.create(item)
    assert manager.cancel_sync(item["id"])["state"] == "cancelled"
    SourceJobRunner(manager).tick()
    assert manager.list()[0]["chunks"] == 0
    next_job = manager.jobs.create(item)
    manager.update(item["id"], 1, name="Renamed", config=item["config"], enabled=True)
    SourceJobRunner(manager).tick()
    assert manager.jobs.get(next_job["id"])["state"] == "cancelled"
    assert manager.jobs.get(queued["id"])["state"] == "cancelled"
    assert manager.list()[0]["chunks"] == 0


def test_crashed_running_job_is_interrupted_and_preserves_checkpoint(manager, tmp_path):
    item = source(manager, tmp_path)
    manager.sync(item["id"])
    checkpoint = manager.list()[0]["checkpoint"].copy()
    queued = manager.jobs.create(item)
    manager.jobs.claim(queued["id"])
    SourceJobRunner(manager).tick()
    assert manager.jobs.get(queued["id"])["state"] == "interrupted"
    assert manager.list()[0]["checkpoint"] == checkpoint
    assert manager.list()[0]["chunks"] == 1


def test_live_worker_lease_prevents_recovery_and_duplicate_run(
    manager, tmp_path, monkeypatch
):
    item = source(manager, tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = LocalFilesConnector.sync

    def blocked(reader, **kwargs):
        entered.set()
        assert release.wait(5)
        yield from original(reader, **kwargs)

    monkeypatch.setattr(LocalFilesConnector, "sync", blocked)
    job = manager.start_sync(item["id"])
    try:
        assert entered.wait(2)
        other = SourceManager(manager.store, knowledge_path=manager.knowledge_path)
        SourceJobRunner(other).tick()
        assert other.jobs.get(job["id"])["state"] == "running"
        with pytest.raises(SourceConflict, match="busy"):
            other.start_sync(item["id"])
        assert other.cancel_sync(item["id"])["cancel_requested"]
    finally:
        release.set()
    assert wait_job(manager, job["id"])["state"] == "cancelled"
    assert manager.list()[0]["chunks"] == 0
    assert manager.list()[0]["checkpoint"]["last_sync"] is None


def test_cross_manager_capacity_is_global_and_third_schedule_stays_due(
    manager, tmp_path, monkeypatch
):
    items = [source(manager, tmp_path, f"Folder {number}") for number in range(3)]
    entered, release = threading.Event(), threading.Event()
    original = LocalFilesConnector.sync
    count = 0
    guard = threading.Lock()

    def blocked(reader, **kwargs):
        nonlocal count
        with guard:
            count += 1
            if count == 2:
                entered.set()
        assert release.wait(5)
        yield from original(reader, **kwargs)

    monkeypatch.setattr(LocalFilesConnector, "sync", blocked)
    first = manager.start_sync(items[0]["id"])
    other = SourceManager(manager.store, knowledge_path=manager.knowledge_path)
    second = other.start_sync(items[1]["id"])
    try:
        assert entered.wait(2)
        now = datetime.now(timezone.utc)
        # Simulate another process's independent semaphore: SQLite must still
        # enforce capacity while the first two workers retain their own slots.
        monkeypatch.setattr(
            "openjarvis.connectors.source_manager._SYNC_SLOTS",
            threading.BoundedSemaphore(2),
        )
        with pytest.raises(SourceConflict, match="syncing"):
            other.start_sync(items[2]["id"])
        manager.jobs.set_schedule(
            items[2]["id"], 0, enabled=True, interval_seconds=300, now=now
        )
        SourceJobRunner(manager).tick(now + timedelta(seconds=301))
        assert manager.jobs.history(items[2]["id"]) == []
        assert manager.jobs.due(now + timedelta(seconds=301))
    finally:
        release.set()
    assert wait_job(manager, first["id"])["state"] == "succeeded"
    assert wait_job(manager, second["id"])["state"] == "succeeded"
    other.stop_jobs()


def test_cancel_during_authenticated_fetch_keeps_previous_delta_token(
    manager, monkeypatch
):
    credential = manager.credentials.create(
        "API", "bearer", "https://api.example.test", "protected-test-secret-long"
    )
    item = manager.create(
        "json_api",
        "API",
        {
            "url": "https://api.example.test/data",
            "credential_id": credential["id"],
            "mode": "records",
            "records_pointer": "/items",
            "sync_mode": "incremental",
            "sync_token_pointer": "/token",
        },
    )
    entered, release = threading.Event(), threading.Event()
    blocked = False

    def fetch(url, **kwargs):
        if blocked:
            entered.set()
            assert release.wait(5)
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "items": [{"id": "a", "body": "Approved"}],
                    "token": "old" if not blocked else "new",
                }
            ).encode(),
            headers={"content-type": "application/json"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("openjarvis.connectors.web_sources.fetch_public_source", fetch)
    manager.sync(item["id"])
    before = manager.list()[0]["checkpoint"]
    blocked = True
    job = manager.start_sync(item["id"])
    try:
        assert entered.wait(2)
        manager.cancel_sync(item["id"])
    finally:
        release.set()
    result = wait_job(manager, job["id"])
    assert result["state"] == "cancelled"
    assert "protected-test-secret-long" not in json.dumps(result)
    assert manager.list()[0]["checkpoint"]["cursor"] == before["cursor"]
    assert manager.list()[0]["checkpoint"]["last_sync"] == before["last_sync"]


def test_committing_job_rejects_cancel_and_finishes_consistently(
    manager, tmp_path, monkeypatch
):
    item = source(manager, tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = KnowledgeStore.reconcile_document_prefix

    def blocked(store, *args):
        entered.set()
        assert release.wait(5)
        return original(store, *args)

    monkeypatch.setattr(KnowledgeStore, "reconcile_document_prefix", blocked)
    job = manager.start_sync(item["id"])
    try:
        assert entered.wait(2)
        assert manager.jobs.get(job["id"])["phase"] == "committing"
        with pytest.raises(SourceConflict, match="too late"):
            manager.cancel_sync(item["id"])
    finally:
        release.set()
    assert wait_job(manager, job["id"])["state"] == "succeeded"
    assert manager.list()[0]["checkpoint"]["last_sync"]


def test_removing_source_removes_schedule_and_run_history(manager, tmp_path):
    item = source(manager, tmp_path)
    manager.jobs.set_schedule(item["id"], 0, enabled=True, interval_seconds=300)
    manager.jobs.create(item)
    manager.jobs.cancel(item["id"])
    manager.delete(item["id"], 1)
    with manager.store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM source_schedules").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM source_jobs").fetchone()[0] == 0


def test_failed_thread_start_releases_leases_and_allows_retry(
    manager, tmp_path, monkeypatch
):
    item = source(manager, tmp_path)
    with monkeypatch.context() as patch:

        def fail_start(worker):
            raise RuntimeError("thread start failed")

        patch.setattr(threading.Thread, "start", fail_start)
        with pytest.raises(RuntimeError, match="thread start failed"):
            manager.start_sync(item["id"])
    assert manager.jobs.history(item["id"])[0]["state"] == "failed"
    assert not manager._workers
    job = manager.start_sync(item["id"])
    assert wait_job(manager, job["id"])["state"] == "succeeded"


def test_scheduler_lease_and_shutdown_prevent_dispatch(manager, tmp_path):
    item = source(manager, tmp_path)
    now = datetime.now(timezone.utc)
    manager.jobs.set_schedule(
        item["id"], 0, enabled=True, interval_seconds=300, now=now
    )
    first, other = SourceJobRunner(manager), SourceJobRunner(manager)
    with first._leader():
        other.tick(now + timedelta(seconds=301))
    assert manager.jobs.history(item["id"]) == []
    other.stop()
    other.tick(now + timedelta(seconds=301))
    assert manager.jobs.history(item["id"]) == []
    first.tick(now + timedelta(seconds=301))
    job = manager.jobs.history(item["id"])[0]
    assert wait_job(manager, job["id"])["state"] == "succeeded"
