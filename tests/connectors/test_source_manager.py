"""Source isolation, crash recovery, migration and real indexed-data lifecycle."""

import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from openjarvis.connectors.local_files import LocalFilesConnector
from openjarvis.connectors.pipeline import IngestionPipeline
from openjarvis.connectors.source_adapters import get_adapter
from openjarvis.connectors.source_manager import SourceManager
from openjarvis.connectors.source_store import SourceConflict, SourceStore
from openjarvis.connectors.store import KnowledgeStore


@pytest.fixture
def manager(tmp_path, monkeypatch):
    store = SourceStore(
        str(tmp_path / "sources.db"), legacy_path=str(tmp_path / "legacy.json")
    )
    # Index-lifecycle tests exercise administrative retrieval over their test DB.
    monkeypatch.setattr(
        "openjarvis.connectors.source_access.allowed_source_ids",
        lambda: {row["id"] for row in store.list()},
    )
    return SourceManager(store, knowledge_path=str(tmp_path / "knowledge.db"))


def create(manager, tmp_path, name="Documents"):
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    (root / "policy.txt").write_text("Migration approved in September")
    return manager.create("local_files", name, {"path": str(root)})


def test_multiple_instances_of_same_folder_do_not_share_index_or_checkpoint(
    manager, tmp_path
):
    one = create(manager, tmp_path)
    two = manager.create("local_files", "Second", one["config"])
    assert manager.sync(one["id"]) == 1
    assert manager.sync(two["id"]) == 1
    records = manager.list()
    assert [r["chunks"] for r in records] == [1, 1]
    assert all(r["checkpoint"]["last_sync"] for r in records)
    with KnowledgeStore(manager.knowledge_path) as knowledge:
        rows = knowledge._conn.execute(
            "SELECT doc_id,metadata FROM knowledge_chunks"
        ).fetchall()
        assert len({row["doc_id"] for row in rows}) == 2
        assert {json.loads(row["metadata"])["source_instance_id"] for row in rows} == {
            one["id"],
            two["id"],
        }
    manager.delete(one["id"], one["revision"])
    assert manager.list()[0]["chunks"] == 1
    assert (Path(one["config"]["path"]) / "policy.txt").exists()


def test_changed_and_deleted_files_are_reconciled_after_success(manager, tmp_path):
    source = create(manager, tmp_path)
    root = Path(source["config"]["path"])
    manager.sync(source["id"])
    (root / "policy.txt").write_text("Migration canceled in September")
    manager.sync(source["id"])
    with KnowledgeStore(manager.knowledge_path) as knowledge:
        assert "canceled" in knowledge.retrieve("Migration")[0].content
    (root / "policy.txt").unlink()
    manager.sync(source["id"])
    assert manager.list()[0]["chunks"] == 0


def test_failed_snapshot_does_not_remove_unreached_documents(manager, tmp_path):
    source = create(manager, tmp_path)
    root = Path(source["config"]["path"])
    manager.sync(source["id"])
    (root / "policy.txt").unlink()
    (root / "bad.txt").write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        manager.sync(source["id"])
    record = manager.list()[0]
    assert record["chunks"] == 1
    assert record["state"] == "error"
    assert record["checkpoint"]["error"]


def test_edit_path_clears_only_own_index_and_stale_revisions_do_nothing(
    manager, tmp_path
):
    one = create(manager, tmp_path, "One")
    two = create(manager, tmp_path, "Two")
    manager.sync(one["id"])
    manager.sync(two["id"])
    new = tmp_path / "New"
    new.mkdir()
    (new / "policy.txt").write_text("New policy")
    updated = manager.update(
        one["id"], one["revision"], name="New", config={"path": str(new)}, enabled=True
    )
    assert updated["revision"] == 2
    with pytest.raises(SourceConflict):
        manager.update(one["id"], 1, name="Old", config=one["config"], enabled=True)
    assert {r["name"]: r["chunks"] for r in manager.list()} == {"New": 0, "Two": 1}
    manager.sync(one["id"])
    assert {r["name"]: r["chunks"] for r in manager.list()} == {"New": 1, "Two": 1}
    with pytest.raises(SourceConflict):
        manager.delete(one["id"], 1)
    assert len(manager.list()) == 2


def test_disable_blocks_sync_and_tool_reads_but_preserves_index(manager, tmp_path):
    source = create(manager, tmp_path)
    manager.sync(source["id"])
    manager.update(
        source["id"], 1, name=source["name"], config=source["config"], enabled=False
    )
    with pytest.raises(SourceConflict, match="disabled"):
        manager.sync(source["id"])
    assert list(manager.collect("local_files")) == []
    assert manager.list()[0]["chunks"] == 1


def test_restart_preserves_sources_and_marks_dead_workers_interrupted(
    manager, tmp_path
):
    source = create(manager, tmp_path)
    manager.store.set_status(source["id"], "syncing")
    reopened = SourceManager(
        SourceStore(str(manager.store.path), legacy_path=str(tmp_path / "absent")),
        knowledge_path=manager.knowledge_path,
    )
    assert reopened.list()[0]["error"] == "Sync interrupted; retry"
    assert reopened.sync(source["id"]) == 1
    assert reopened.list()[0]["state"] == "idle"


def test_worker_ownership_rejects_cross_manager_mutations(
    manager, tmp_path, monkeypatch
):
    source = create(manager, tmp_path)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = LocalFilesConnector.sync
    original_status = manager.store.set_status

    def blocked(reader, **kwargs):
        started.set()
        assert release.wait(5)
        yield from original(reader, **kwargs)

    def status(source_id, state, error=None):
        original_status(source_id, state, error)
        if state != "syncing":
            finished.set()

    monkeypatch.setattr(LocalFilesConnector, "sync", blocked)
    monkeypatch.setattr(manager.store, "set_status", status)
    other = SourceManager(manager.store, knowledge_path=manager.knowledge_path)
    manager.start_sync(source["id"])
    try:
        assert started.wait(2)
        assert other.list()[0]["state"] == "syncing"
        for operation in (
            lambda: other.delete(source["id"], 1),
            lambda: other.update(
                source["id"], 1, name="Changed", config=source["config"], enabled=False
            ),
            lambda: other.start_sync(source["id"]),
        ):
            with pytest.raises(SourceConflict, match="busy"):
                operation()
    finally:
        release.set()
        assert finished.wait(3)


def test_migration_retains_document_ids_and_is_idempotent(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    (root / "policy.txt").write_text("Migration approved")
    legacy = tmp_path / "legacy.json"
    reader = LocalFilesConnector(config_path=str(legacy))
    reader.configure_path(str(root))
    knowledge_path = str(tmp_path / "knowledge.db")
    with KnowledgeStore(knowledge_path) as knowledge:
        IngestionPipeline(knowledge).ingest(reader.sync())
        old_id = knowledge._conn.execute(
            "SELECT doc_id FROM knowledge_chunks"
        ).fetchone()[0]
    store = SourceStore(str(tmp_path / "sources.db"), legacy_path=str(legacy))
    source = store.list()[0]
    assert legacy.with_suffix(".json.migrated").exists()
    manager = SourceManager(store, knowledge_path=knowledge_path)
    manager.sync(source["id"])
    with KnowledgeStore(knowledge_path) as knowledge:
        assert knowledge.count() == 1
        row = knowledge._conn.execute(
            "SELECT doc_id,metadata FROM knowledge_chunks"
        ).fetchone()
        assert row["doc_id"] == old_id
        assert json.loads(row["metadata"])["source_instance_id"] == source["id"]
    legacy.write_text(json.dumps({"path": str(root)}))
    assert len(SourceStore(str(store.path), legacy_path=str(legacy)).list()) == 1
    manager.delete(source["id"], 1)
    assert manager.list() == []


def test_migration_preserves_temporarily_unmounted_folder(tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"path": str(tmp_path / "unmounted")}))
    store = SourceStore(str(tmp_path / "sources.db"), legacy_path=str(legacy))
    assert store.list()[0]["config"]["path"].endswith("unmounted")


def test_unsupported_config_version_fails_closed(manager, tmp_path):
    source = create(manager, tmp_path)
    with manager.store.connection() as conn:
        conn.execute(
            "UPDATE sources SET config_version=999 WHERE id=?", (source["id"],)
        )
    with pytest.raises(ValueError, match="version migration"):
        manager.sync(source["id"])


def test_adapter_definition_drives_validation_and_creation(
    manager, tmp_path, monkeypatch
):
    from openjarvis.connectors import source_adapters

    base = get_adapter("local_files")
    adapter = replace(base, adapter_id="future_files", display_name="Future Files")
    monkeypatch.setitem(source_adapters._ADAPTERS, adapter.adapter_id, adapter)
    root = tmp_path / "root"
    root.mkdir()
    result = manager.create("future_files", "Future", {"path": str(root)})
    assert result["adapter_id"] == "future_files"
    assert any(
        a["adapter_id"] == "future_files" for a in source_adapters.list_adapters()
    )
    with pytest.raises(ValueError, match="unsupported"):
        manager.create("future_files", "Bad", {"path": str(root), "token": "secret"})
    assert len(manager.list()) == 1


def test_replaced_root_symlink_cannot_expand_scope(manager, tmp_path):
    source = create(manager, tmp_path)
    root = Path(source["config"]["path"])
    (root / "policy.txt").unlink()
    root.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("Must not read")
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="directory changed"):
        manager.sync(source["id"])
    assert manager.list()[0]["chunks"] == 0


def test_registry_restoration_preserves_management_types_and_adapter_registry():
    from openjarvis.connectors import (
        ensure_connectors_populated,
        source_adapters,
        source_manager,
        source_store,
    )

    store_class = source_store.SourceStore
    manager_class = source_manager.SourceManager
    conflict_class = source_store.SourceConflict
    adapter = source_adapters.get_adapter("local_files")
    ensure_connectors_populated()
    assert source_store.SourceStore is store_class
    assert source_store.SourceConflict is conflict_class
    assert source_manager.SourceManager is manager_class
    assert source_adapters.get_adapter("local_files") is adapter


def test_instance_identity_survives_into_search_evidence(manager, tmp_path):
    from openjarvis.tools.knowledge_search import KnowledgeSearchTool

    source = create(manager, tmp_path)
    manager.sync(source["id"])
    with KnowledgeStore(manager.knowledge_path) as knowledge:
        result = KnowledgeSearchTool(knowledge).execute(query="Migration")
    record = result.metadata["evidence"]["records"][0]
    assert record["metadata"]["source_instance_id"] == source["id"]
    assert record["metadata"]["source_instance_name"] == source["name"]


def test_background_sync_capacity_is_bounded(manager, tmp_path, monkeypatch):
    from openjarvis.connectors import source_manager

    source = create(manager, tmp_path)
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    monkeypatch.setattr(source_manager, "_SYNC_SLOTS", slots)
    with pytest.raises(SourceConflict, match="syncing"):
        manager.start_sync(source["id"])
    slots.release()
    # Rejection released the connection lock and left its state unchanged.
    assert manager.list()[0]["state"] == "idle"
    assert manager.sync(source["id"]) == 1
