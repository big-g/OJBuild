"""Explicit version upgrades and value-free transactional configuration history."""

import json
import sqlite3
from dataclasses import replace

import pytest

from openjarvis.connectors.source_adapters import (
    ConfigMigration,
    get_adapter,
    register_adapter,
)
from openjarvis.connectors.source_audit import list_events
from openjarvis.connectors.source_manager import SourceManager
from openjarvis.connectors.source_store import SourceConflict, SourceStore


@pytest.fixture
def manager(tmp_path):
    manager = SourceManager(
        SourceStore(str(tmp_path / "sources.db"), legacy_path=str(tmp_path / "absent")),
        knowledge_path=str(tmp_path / "knowledge.db"),
    )
    yield manager
    manager.stop_jobs()


def upgrade(manager, tmp_path, monkeypatch, *, preserves=False):
    root = tmp_path / "documents"
    root.mkdir()
    (root / "policy.txt").write_text("Migration approved")
    old = manager.create("local_files", "Private source name", {"path": str(root)})
    manager.sync(old["id"])
    adapter = replace(
        get_adapter("local_files"),
        config_version=3,
        migrations=(
            ConfigMigration(1, lambda cfg: {**cfg, "temporary": True}, preserves),
            ConfigMigration(2, lambda cfg: {"path": cfg["path"]}, preserves),
        ),
    )
    monkeypatch.setitem(
        __import__(
            "openjarvis.connectors.source_adapters", fromlist=["_ADAPTERS"]
        )._ADAPTERS,
        "local_files",
        adapter,
    )
    return old, adapter


def test_preview_is_read_only_and_explicit_upgrade_preserves_index(
    manager, tmp_path, monkeypatch
):
    old, adapter = upgrade(manager, tmp_path, monkeypatch, preserves=True)
    before = manager.list()[0]
    plan = manager.migration_preview(old["id"], 1)
    assert plan["from_version"] == 1 and plan["to_version"] == 3
    assert not plan["index_reset"]
    assert manager.store.get(old["id"])["config_version"] == 1
    assert len(list_events(manager.store)) == 1
    assert manager.list()[0]["configuration_state"] == "migration_available"
    result = manager.migrate(old["id"], 1, plan["plan_token"], actor="user:owner")
    assert result["config_version"] == 3 and result["revision"] == 2
    current = manager.list()[0]
    assert current["chunks"] == before["chunks"] == 1
    assert current["checkpoint"] == before["checkpoint"]
    assert current["configuration_state"] == "current"
    event = list_events(manager.store)[0]
    assert event["action"] == "migrated" and event["actor"] == "user:owner"
    assert event["previous_version"] == 1 and not event["index_reset"]


def test_default_upgrade_resets_only_own_index_and_keeps_schedule(
    manager, tmp_path, monkeypatch
):
    old, adapter = upgrade(manager, tmp_path, monkeypatch)
    manager.jobs.set_schedule(old["id"], 0, enabled=True, interval_seconds=300)
    other = manager.create("local_files", "Other", old["config"])
    manager.sync(other["id"])
    plan = manager.migration_preview(old["id"], 1)
    assert plan["index_reset"]
    manager.migrate(old["id"], 1, plan["plan_token"])
    records = {item["id"]: item for item in manager.list()}
    assert records[old["id"]]["chunks"] == 0
    assert records[old["id"]]["checkpoint"] is None
    assert records[other["id"]]["chunks"] == 1
    assert records[old["id"]]["schedule"]["enabled"]
    with pytest.raises(SourceConflict, match="changed"):
        manager.migrate(old["id"], 1, plan["plan_token"])


def test_recomputed_plan_rejects_changed_adapter_without_purging(
    manager, tmp_path, monkeypatch
):
    old, adapter = upgrade(manager, tmp_path, monkeypatch)
    plan = manager.migration_preview(old["id"], 1)
    from openjarvis.connectors import source_adapters

    monkeypatch.setitem(
        source_adapters._ADAPTERS,
        "local_files",
        replace(
            adapter,
            migrations=tuple(
                replace(step, preserves_index=True) for step in adapter.migrations
            ),
        ),
    )
    with pytest.raises(SourceConflict, match="preview changed"):
        manager.migrate(old["id"], 1, plan["plan_token"])
    assert manager.list()[0]["chunks"] == 1
    assert len(list_events(manager.store)) == 1


@pytest.mark.parametrize("version,steps", [(4, ()), (0, ()), (1, ())])
def test_missing_path_or_future_version_fails_closed(
    manager, tmp_path, monkeypatch, version, steps
):
    old, adapter = upgrade(manager, tmp_path, monkeypatch)
    from openjarvis.connectors import source_adapters

    monkeypatch.setitem(
        source_adapters._ADAPTERS, "local_files", replace(adapter, migrations=steps)
    )
    with manager.store.connection() as conn:
        conn.execute(
            "UPDATE sources SET config_version=? WHERE id=?", (version, old["id"])
        )
    assert manager.list()[0]["configuration_state"] == "unsupported"
    with pytest.raises(ValueError):
        manager.migration_preview(old["id"], 1)
    with pytest.raises(ValueError, match="migration"):
        manager.start_sync(old["id"])
    assert manager.jobs.history(old["id"]) == []
    assert manager.list()[0]["chunks"] == 1
    manager.update(old["id"], 1, name=old["name"], config=old["config"], enabled=False)


def test_busy_source_cannot_preview_or_apply(manager, tmp_path, monkeypatch):
    old, adapter = upgrade(manager, tmp_path, monkeypatch)
    with manager._locked(old["id"]):
        with pytest.raises(SourceConflict, match="busy"):
            manager.migration_preview(old["id"], 1)
        with pytest.raises(SourceConflict, match="busy"):
            manager.migrate(old["id"], 1, "0" * 64)


def test_hook_errors_never_echo_configuration_and_do_not_mutate(
    manager, tmp_path, monkeypatch
):
    old, adapter = upgrade(manager, tmp_path, monkeypatch)
    from openjarvis.connectors import source_adapters

    def fail(cfg):
        cfg["path"] = "modified"
        raise RuntimeError("secret-looking-config-value")

    monkeypatch.setitem(
        source_adapters._ADAPTERS,
        "local_files",
        replace(adapter, migrations=(ConfigMigration(1, fail),)),
    )
    with pytest.raises(ValueError, match="migration failed") as error:
        manager.migration_preview(old["id"], 1)
    assert "secret-looking" not in str(error.value)
    assert manager.store.get(old["id"])["config"] == old["config"]
    assert len(list_events(manager.store)) == 1


def test_migration_cannot_rebind_credentials():
    adapter = replace(
        get_adapter("web_page"),
        config_version=2,
        migrations=(
            ConfigMigration(
                1,
                lambda cfg: {
                    **cfg,
                    "credential_id": "00000000-0000-4000-8000-000000000001",
                },
            ),
        ),
    )
    with pytest.raises(ValueError, match="credential references"):
        adapter.migrate_config(
            {
                "url": "https://example.test/data",
                "credential_id": "00000000-0000-4000-8000-000000000002",
            },
            1,
        )


@pytest.mark.parametrize(
    "steps",
    [
        (ConfigMigration(1, lambda cfg: cfg), ConfigMigration(1, lambda cfg: cfg)),
        (ConfigMigration(True, lambda cfg: cfg),),
        (ConfigMigration(2, lambda cfg: cfg),),
        (ConfigMigration(1, None),),
        (ConfigMigration(1, lambda cfg: cfg, "yes"),),
    ],
)
def test_registration_rejects_ambiguous_or_invalid_steps(steps):
    with pytest.raises(ValueError, match="migration steps"):
        register_adapter(
            replace(
                get_adapter("local_files"),
                adapter_id="invalid-evolution",
                config_version=2,
                migrations=steps,
            )
        )


def test_audit_metadata_survives_delete_and_paginates_without_values(manager, tmp_path):
    root = tmp_path / "secret-folder"
    root.mkdir()
    item = manager.create(
        "local_files", "Sensitive name", {"path": str(root)}, actor="user:owner"
    )
    manager.update(
        item["id"], 1, name="Other sensitive name", config=item["config"], enabled=False
    )
    manager.jobs.set_schedule(
        item["id"], 0, enabled=True, interval_seconds=300, actor="server_access"
    )
    with pytest.raises(SourceConflict):
        manager.update(
            item["id"], 1, name="Stale", config=item["config"], enabled=False
        )
    manager.delete(item["id"], 2, actor="user:owner")
    events = list_events(manager.store, item["id"])
    assert [e["action"] for e in events] == [
        "removed",
        "schedule_updated",
        "updated",
        "created",
    ]
    assert events[1]["schedule_revision"] == 1 and events[1]["revision"] == 2
    serialized = json.dumps(events)
    assert "Sensitive" not in serialized and str(root) not in serialized
    first = list_events(manager.store, limit=2)
    second = list_events(manager.store, before_id=first[-1]["id"], limit=2)
    assert first + second == events
    reopened = SourceStore(
        str(manager.store.path), legacy_path=str(tmp_path / "absent")
    )
    assert list_events(reopened) == events


def test_audit_failure_rolls_back_source_and_schedule_changes(manager, tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    item = manager.create("local_files", "Original", {"path": str(root)})
    with manager.store.connection() as conn:
        conn.execute(
            "CREATE TRIGGER fail_audit BEFORE INSERT ON source_audit "
            "BEGIN SELECT RAISE(ABORT,'test failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        manager.update(
            item["id"], 1, name="Rejected", config=item["config"], enabled=False
        )
    with pytest.raises(sqlite3.IntegrityError):
        manager.jobs.set_schedule(item["id"], 0, enabled=True, interval_seconds=300)
    assert manager.store.get(item["id"])["revision"] == 1
    assert not manager.jobs.schedule(item["id"])["enabled"]
    assert len(list_events(manager.store)) == 1


def test_schema_two_upgrade_retains_sources_and_does_not_invent_history(
    manager, tmp_path
):
    root = tmp_path / "documents"
    root.mkdir()
    item = manager.create("local_files", "Old source", {"path": str(root)})
    with manager.store.connection() as conn:
        conn.execute("DROP TABLE source_audit")
        conn.execute("PRAGMA user_version=2")
    reopened = SourceStore(
        str(manager.store.path), legacy_path=str(tmp_path / "absent")
    )
    assert reopened.get(item["id"])["config"] == item["config"]
    assert list_events(reopened) == []
    with reopened.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_legacy_upgrade_converts_path_bound_identities_before_fields_evolve(
    tmp_path, monkeypatch
):
    root = tmp_path / "documents"
    root.mkdir()
    (root / "policy.txt").write_text("Migration approved")
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"path": str(root)}))
    manager = SourceManager(
        SourceStore(str(tmp_path / "sources.db"), legacy_path=str(legacy)),
        knowledge_path=str(tmp_path / "knowledge.db"),
    )
    old = manager.store.list()[0]
    manager.sync(old["id"])
    from openjarvis.connectors import source_adapters

    adapter = replace(
        get_adapter("local_files"),
        config_version=2,
        migrations=(ConfigMigration(1, lambda cfg: cfg, preserves_index=True),),
    )
    monkeypatch.setitem(source_adapters._ADAPTERS, "local_files", adapter)
    plan = manager.migration_preview(old["id"], 1)
    assert plan["index_reset"]  # Safe conversion overrides preservation.
    manager.migrate(old["id"], 1, plan["plan_token"])
    assert not manager.store.get(old["id"])["legacy_document_ids"]
    assert manager.list()[0]["chunks"] == 0
    assert list_events(manager.store)[-1]["action"] == "legacy_imported"
    assert "document_identity" in list_events(manager.store)[0]["changed_fields"]
    manager.sync(old["id"])
    assert manager.list()[0]["chunks"] == 1


def test_removed_adapter_can_be_disabled_without_losing_settings(
    manager, tmp_path, monkeypatch
):
    old, adapter = upgrade(manager, tmp_path, monkeypatch)
    from openjarvis.connectors import source_adapters

    monkeypatch.delitem(source_adapters._ADAPTERS, "local_files")
    result = manager.update(
        old["id"], 1, name="Paused", config=old["config"], enabled=False
    )
    assert not result["enabled"] and result["config"] == old["config"]
    assert manager.list()[0]["chunks"] == 1
    assert manager.list()[0]["configuration_state"] == "unsupported"


def test_incompatible_schedule_waits_without_blocking_compatible_jobs(
    manager, tmp_path, monkeypatch
):
    from datetime import datetime, timedelta, timezone

    from openjarvis.connectors.source_job_runner import SourceJobRunner

    old, adapter = upgrade(manager, tmp_path, monkeypatch)
    other = manager.create("local_files", "Current", old["config"])
    now = datetime.now(timezone.utc)
    for item in (old, other):
        manager.jobs.set_schedule(
            item["id"], 0, enabled=True, interval_seconds=300, now=now
        )
    # Keep dispatch deterministic and inspect acceptance without launching IO.
    started = []
    original = manager.start_sync

    def start(source_id, **kwargs):
        if source_id == other["id"]:
            started.append(source_id)
            return {}
        return original(source_id, **kwargs)

    monkeypatch.setattr(manager, "start_sync", start)
    SourceJobRunner(manager).tick(now + timedelta(seconds=301))
    assert started == [other["id"]]
    assert manager.jobs.history(old["id"]) == []
    assert {
        row["source_id"] for row in manager.jobs.due(now + timedelta(seconds=301))
    } == {old["id"], other["id"]}


def test_migration_checks_credential_binding_without_unlocking_or_fetching(
    manager, monkeypatch
):
    from openjarvis.connectors import source_adapters

    credential = manager.credentials.create(
        "API", "bearer", "https://api.example.test", "protected-test-secret-long"
    )
    item = manager.create(
        "web_page",
        "Private",
        {"url": "https://api.example.test/page", "credential_id": credential["id"]},
    )
    adapter = replace(
        get_adapter("web_page"),
        config_version=2,
        migrations=(
            ConfigMigration(
                1, lambda cfg: {**cfg, "url": "https://other.example.test/page"}
            ),
        ),
    )
    monkeypatch.setitem(source_adapters._ADAPTERS, "web_page", adapter)

    def forbidden(*args, **kwargs):
        pytest.fail("Preview must not decrypt or fetch")

    monkeypatch.setattr(manager.credentials, "material", forbidden)
    monkeypatch.setattr(
        "openjarvis.connectors.web_sources.fetch_public_source", forbidden
    )
    with pytest.raises(ValueError, match="origin"):
        manager.migration_preview(item["id"], 1)
    assert manager.store.get(item["id"])["config_version"] == 1
    assert len(list_events(manager.store)) == 1
