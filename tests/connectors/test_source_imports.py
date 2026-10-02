"""Legacy imports are local, previewed, secret-free and recoverable."""

import concurrent.futures
import json
from dataclasses import replace

import httpx
import pytest

from openjarvis.connectors import source_adapters
from openjarvis.connectors.oauth import delete_tokens, load_tokens, save_tokens
from openjarvis.connectors.source_audit import list_events
from openjarvis.connectors.source_imports import SourceImports
from openjarvis.connectors.source_manager import SourceManager
from openjarvis.connectors.source_store import SourceConflict, SourceStore

TOKEN = "legacy-notion-secret-example"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("Imports must not contact a provider")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)


@pytest.fixture
def manager(tmp_path):
    return SourceManager(
        SourceStore(
            str(tmp_path / "sources.db"), legacy_path=str(tmp_path / "missing")
        ),
        knowledge_path=str(tmp_path / "knowledge.db"),
    )


@pytest.fixture
def legacy(manager):
    path = manager.store.path.parent / "connectors" / "notion.json"
    save_tokens(str(path), {"token": TOKEN})
    return path


def plan(manager):
    return SourceImports(manager).preview("notion", "Work", actor="user:owner")


def apply(manager, preview):
    return SourceImports(manager).apply(
        "notion", preview["plan_token"], actor="user:owner"
    )


def test_preview_is_secret_free_and_does_not_copy_credentials_or_create_source(
    manager, legacy
):
    original = legacy.read_bytes()
    preview = plan(manager)
    assert TOKEN not in json.dumps(preview)
    assert "fingerprint" not in preview
    assert preview["fresh_index"] and preview["legacy_connection_kept"]
    assert preview["config"]["max_pages"] == 100
    assert preview["config_version"] == 1
    assert legacy.read_bytes() == original
    assert manager.store.list() == []
    assert manager.credentials.list() == []
    assert list_events(manager.store) == []


def test_apply_and_replay_create_only_one_source_and_credential(manager, legacy):
    preview = plan(manager)
    original = legacy.read_bytes()
    source = apply(manager, preview)
    assert source == apply(manager, preview)
    assert source["adapter_id"] == "notion_pages"
    assert source["name"] == "Work"
    assert not source["legacy_document_ids"]
    assert len(manager.store.list()) == len(manager.credentials.list()) == 1
    assert manager.list()[0]["chunks"] == 0
    assert legacy.read_bytes() == original
    assert load_tokens(str(legacy)) == {"token": TOKEN}
    assert SourceImports(manager).list()[0]["state"] == "imported"
    (event,) = list_events(manager.store)
    assert event["action"] == "imported" and event["actor"] == "user:owner"
    assert TOKEN not in json.dumps(event)
    assert TOKEN.encode() not in manager.store.path.read_bytes()
    assert TOKEN.encode() not in manager.credentials.path.read_bytes()
    with manager.credentials.bound(
        source["config"]["credential_id"], "https://api.notion.com", ("bearer",)
    ) as row:
        assert manager.credentials.material(row)["secret"] == TOKEN


def test_plaintext_preview_is_read_only_and_apply_upgrades_the_legacy_file(manager):
    legacy = manager.store.path.parent / "connectors" / "notion.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({"token": TOKEN}))
    original = legacy.read_bytes()
    preview = plan(manager)
    assert legacy.read_bytes() == original
    apply(manager, preview)
    assert TOKEN.encode() not in legacy.read_bytes()
    assert load_tokens(str(legacy)) == {"token": TOKEN}


def test_changed_or_disconnected_legacy_connection_rejects_stale_preview(
    manager, legacy
):
    preview = plan(manager)
    save_tokens(str(legacy), {"token": "changed-example-token"})
    with pytest.raises(SourceConflict, match="changed"):
        apply(manager, preview)
    assert manager.store.list() == manager.credentials.list() == []
    delete_tokens(str(legacy))
    with pytest.raises(SourceConflict, match="changed"):
        apply(manager, preview)


@pytest.mark.parametrize(
    "actor,token",
    [
        ("user:other", None),
        ("user:owner", "invalid-preview-ticket"),
    ],
)
def test_preview_actor_binding_and_invalid_tickets(manager, legacy, actor, token):
    preview = plan(manager)
    with pytest.raises(ValueError, match="invalid or expired"):
        SourceImports(manager).apply(
            "notion", token or preview["plan_token"], actor=actor
        )
    assert manager.store.list() == manager.credentials.list() == []


def test_expired_preview_does_not_import(manager, legacy):
    import time

    preview = plan(manager)
    cipher = manager.credentials._cipher()
    payload = cipher.decrypt(preview["plan_token"].encode())
    preview["plan_token"] = cipher.encrypt_at_time(
        payload, int(time.time()) - 601
    ).decode()
    with pytest.raises(ValueError, match="expired"):
        apply(manager, preview)


def test_adapter_version_change_invalidates_preview(manager, legacy, monkeypatch):
    preview = plan(manager)
    original = source_adapters.get_adapter("notion_pages")
    monkeypatch.setitem(
        source_adapters._ADAPTERS, "notion_pages", replace(original, config_version=2)
    )
    with pytest.raises(ValueError, match="preview again"):
        apply(manager, preview)


def test_restart_recovers_credential_commit_without_duplicate_copies(
    manager, legacy, monkeypatch
):
    preview = plan(manager)
    original = manager.store._insert_source

    def crash(*args, **kwargs):
        raise RuntimeError("simulated interruption after credential commit")

    monkeypatch.setattr(manager.store, "_insert_source", crash)
    with pytest.raises(RuntimeError):
        apply(manager, preview)
    assert manager.store.list() == []
    (credential,) = manager.credentials.list()
    reopened = SourceManager(
        SourceStore(
            str(manager.store.path), legacy_path=str(legacy.parent / "missing")
        ),
        knowledge_path=manager.knowledge_path,
    )
    source = apply(reopened, preview)
    assert source["config"]["credential_id"] == credential["id"]
    assert len(reopened.credentials.list()) == 1
    monkeypatch.setattr(manager.store, "_insert_source", original)


def test_source_commit_rollback_does_not_publish_a_partial_source(
    manager, legacy, monkeypatch
):
    preview = plan(manager)
    original = manager.store._insert_source

    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("simulated source commit failure")

    monkeypatch.setattr(manager.store, "_insert_source", fail_after_insert)
    with pytest.raises(ValueError):
        apply(manager, preview)
    assert manager.store.list() == []
    assert list_events(manager.store) == []
    monkeypatch.setattr(manager.store, "_insert_source", original)
    apply(manager, preview)
    assert len(manager.store.list()) == len(manager.credentials.list()) == 1


def test_recovery_never_overwrites_a_rotated_reserved_credential(
    manager, legacy, monkeypatch
):
    preview = plan(manager)
    original = manager.store._insert_source
    monkeypatch.setattr(
        manager.store,
        "_insert_source",
        lambda *a, **kw: (_ for _ in ()).throw(OSError()),
    )
    with pytest.raises(ValueError):
        apply(manager, preview)
    (credential,) = manager.credentials.list()
    manager.credentials.rotate(credential["id"], 1, "replacement-private-token")
    monkeypatch.setattr(manager.store, "_insert_source", original)
    with pytest.raises(SourceConflict, match="credential changed"):
        apply(manager, preview)
    assert manager.store.list() == []
    assert manager.credentials.list()[0]["revision"] == 2


def test_removed_import_is_not_silently_recreated_by_retry(manager, legacy):
    preview = plan(manager)
    source = apply(manager, preview)
    manager.delete(source["id"], 1)
    assert SourceImports(manager).list()[0]["state"] == "removed"
    with pytest.raises(SourceConflict, match="removed"):
        apply(manager, preview)


def test_parallel_apply_attempts_never_create_duplicate_sources(manager, legacy):
    preview = plan(manager)

    def attempt():
        try:
            return apply(manager, preview)["id"]
        except SourceConflict:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: attempt(), range(4)))
    assert len({identity for identity in results if identity}) == 1
    assert len(manager.store.list()) == len(manager.credentials.list()) == 1


@pytest.mark.parametrize(
    "raw", ["not-json", "[]", '{"token":""}', '{"client_id":"id"}']
)
def test_unavailable_legacy_objects_are_not_imported(manager, raw):
    path = manager.store.path.parent / "connectors" / "notion.json"
    path.parent.mkdir()
    path.write_text(raw)
    assert SourceImports(manager).list()[0]["state"] == "unavailable"
    with pytest.raises(ValueError, match="unavailable"):
        plan(manager)


def test_missing_key_and_symlink_paths_are_unavailable(manager, legacy, tmp_path):
    manager.credentials.key_path.unlink()
    assert SourceImports(manager).list()[0]["state"] == "unavailable"
    with pytest.raises(ValueError):
        plan(manager)
    legacy.unlink()
    legacy.symlink_to(tmp_path / "another-credential-file")
    assert SourceImports(manager).list()[0]["state"] == "unavailable"


def test_clients_cannot_select_arbitrary_legacy_files_or_integrations(manager, legacy):
    with pytest.raises(ValueError, match="Unsupported"):
        SourceImports(manager).preview("../../outside", "Name")
