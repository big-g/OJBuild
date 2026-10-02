"""Source API authentication, validation, stale edits and adapter metadata."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.connectors.source_manager import SourceManager
from openjarvis.connectors.source_store import SourceStore
from openjarvis.server.auth_middleware import AuthMiddleware
from openjarvis.server.sources_router import create_sources_router


@pytest.fixture
def client(tmp_path):
    manager = SourceManager(
        SourceStore(str(tmp_path / "sources.db"), legacy_path=str(tmp_path / "legacy")),
        knowledge_path=str(tmp_path / "knowledge.db"),
    )
    app = FastAPI()
    app.add_middleware(AuthMiddleware, api_key="test-key")
    app.include_router(create_sources_router(manager))
    with TestClient(app, headers={"Authorization": "Bearer test-key"}) as client:
        yield client


def test_source_api_rejects_unauthenticated_access(client):
    response = client.get("/v1/sources", headers={"Authorization": ""})
    assert response.status_code == 401
    assert client.get("/v1/sources").status_code == 200


def test_named_notion_configuration_is_discovered_and_keeps_credentials_private(client):
    definitions = client.get("/v1/sources/adapters").json()["adapters"]
    notion = next(item for item in definitions if item["adapter_id"] == "notion_pages")
    assert notion["credential_kinds"] == ["bearer"]
    assert "credential:use" in notion["required_capabilities"]
    assert notion["fields"][0]["credential_origin"] == "https://api.notion.com"
    credential = client.post(
        "/v1/sources/credentials",
        json={
            "name": "Notion",
            "kind": "bearer",
            "origin": "https://api.notion.com",
            "secret": "protected-notion-integration-token",
            "header_name": "",
        },
    ).json()
    response = client.post(
        "/v1/sources",
        json={
            "adapter_id": "notion_pages",
            "name": "Work pages",
            "config": {"credential_id": credential["id"]},
        },
    )
    assert response.status_code == 201
    assert response.json()["config"]["credential_id"] == credential["id"]
    for path in ("/v1/sources", "/v1/sources/credentials", "/v1/sources/audit"):
        assert "protected-notion-integration-token" not in client.get(path).text
    rejected = client.post(
        "/v1/sources",
        json={
            "adapter_id": "notion_pages",
            "name": "Bad",
            "config": {"token": "protected-notion-integration-token"},
        },
    )
    assert rejected.status_code == 400
    assert "protected-notion-integration-token" not in rejected.text


def test_adapter_fields_test_configuration_and_crud(client, tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    definitions = client.get("/v1/sources/adapters").json()["adapters"]
    local = next(item for item in definitions if item["adapter_id"] == "local_files")
    assert local["fields"][0]["name"] == "path"
    assert local["required_capabilities"] == [
        "connector:local_files:read",
        "file:read",
    ]
    payload = {"adapter_id": "local_files", "config": {"path": str(root)}}
    assert client.post("/v1/sources/test", json=payload).json()["ok"]
    assert client.get("/v1/sources").json()["sources"] == []
    response = client.post("/v1/sources", json={**payload, "name": "Policies"})
    assert response.status_code == 201
    source = response.json()
    assert "legacy_document_ids" not in source
    update = {
        "name": "Policies 2",
        "config": payload["config"],
        "revision": 1,
        "enabled": False,
    }
    assert client.put(f"/v1/sources/{source['id']}", json=update).status_code == 200
    assert client.put(f"/v1/sources/{source['id']}", json=update).status_code == 409
    assert client.post(f"/v1/sources/{source['id']}/sync").status_code == 409
    assert client.delete(f"/v1/sources/{source['id']}?revision=1").status_code == 409
    assert client.delete(f"/v1/sources/{source['id']}?revision=2").status_code == 204
    assert client.get("/v1/sources").json()["sources"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {"adapter_id": "unknown", "name": "Bad", "config": {}},
        {
            "adapter_id": "local_files",
            "name": "Bad",
            "config": {"path": "/nonexistent-oj-test-folder"},
        },
        {"adapter_id": "local_files", "name": " ", "config": {}},
        {"adapter_id": "local_files", "name": "Bad", "config": {"token": "secret"}},
    ],
)
def test_invalid_config_is_not_persisted(client, payload):
    assert client.post("/v1/sources", json=payload).status_code == 400
    assert client.get("/v1/sources").json()["sources"] == []


def test_invalid_or_missing_instance_id_cannot_create_lock_paths(client):
    assert client.post("/v1/sources/not-a-uuid/sync").status_code == 400
    assert (
        client.post("/v1/sources/00000000-0000-0000-0000-000000000001/sync").status_code
        == 404
    )


def test_background_sync_endpoint_indexes_source(client, tmp_path):
    import time

    root = tmp_path / "documents"
    root.mkdir()
    (root / "policy.txt").write_text("Migration approved")
    source = client.post(
        "/v1/sources",
        json={
            "adapter_id": "local_files",
            "name": "Policies",
            "config": {"path": str(root)},
        },
    ).json()
    response = client.post(f"/v1/sources/{source['id']}/sync")
    assert response.status_code == 202
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        record = client.get("/v1/sources").json()["sources"][0]
        if record["state"] != "syncing":
            assert record["state"] == "idle", record["error"]
            assert record["chunks"] == 1
            assert record["checkpoint"]["last_sync"]
            break
        time.sleep(0.01)
    else:
        pytest.fail("Background source sync did not finish")


def test_credentials_authenticated_metadata_only_and_rotation(client):
    payload = {
        "name": "API",
        "kind": "bearer",
        "origin": "https://api.example.com",
        "secret": "protected-token-123456789",
    }
    assert (
        client.post(
            "/v1/sources/credentials", json=payload, headers={"Authorization": ""}
        ).status_code
        == 401
    )
    result = client.post("/v1/sources/credentials", json=payload)
    assert result.status_code == 201
    assert payload["secret"] not in result.text
    record = result.json()
    listing = client.get("/v1/sources/credentials")
    assert payload["secret"] not in listing.text
    assert "sealed" not in listing.text
    rotation = client.put(
        f"/v1/sources/credentials/{record['id']}",
        json={"revision": 1, "secret": "replacement-protected-token"},
    )
    assert rotation.status_code == 200
    assert "replacement-protected-token" not in rotation.text
    assert (
        client.put(
            f"/v1/sources/credentials/{record['id']}",
            json={"revision": 1, "secret": "stale-token"},
        ).status_code
        == 409
    )
    assert (
        client.delete(f"/v1/sources/credentials/{record['id']}?revision=2").status_code
        == 204
    )


@pytest.mark.parametrize(
    "secret",
    [
        "",
        "secret\nwith-newline",
        ["protected-validation-marker"],
        {"token": "protected-validation-marker"},
    ],
)
def test_credential_validation_never_echoes_secret(client, secret):
    response = client.post(
        "/v1/sources/credentials",
        json={
            "name": "Test",
            "kind": "bearer",
            "origin": "https://api.example.com",
            "secret": secret,
        },
    )
    assert response.status_code in (400, 422)
    assert "protected-validation-marker" not in response.text
    assert "secret\\nwith-newline" not in response.text
    assert client.get("/v1/sources/credentials").json()["credentials"] == []


def test_schedule_history_and_cancel_routes_require_api_authentication(
    client, tmp_path
):
    folder = tmp_path / "scheduled"
    folder.mkdir()
    source = client.post(
        "/v1/sources",
        json={
            "adapter_id": "local_files",
            "name": "Scheduled",
            "config": {"path": str(folder)},
        },
    ).json()
    base = f"/v1/sources/{source['id']}"
    payload = {"revision": 0, "enabled": True, "interval_seconds": 300}
    assert (
        client.put(
            base + "/schedule", json=payload, headers={"Authorization": ""}
        ).status_code
        == 401
    )
    assert client.get(base + "/jobs", headers={"Authorization": ""}).status_code == 401
    assert (
        client.post(base + "/cancel", headers={"Authorization": ""}).status_code == 401
    )
    schedule = client.put(base + "/schedule", json=payload)
    assert schedule.status_code == 200
    assert schedule.json()["revision"] == 1
    assert client.put(base + "/schedule", json=payload).status_code == 409
    assert client.get(base + "/jobs").json()["jobs"] == []
    assert client.post(base + "/cancel").status_code == 409
    listed = client.get("/v1/sources").json()["sources"][0]
    assert listed["schedule"]["enabled"]
    assert listed["revision"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"revision": 0, "enabled": True, "interval_seconds": 299},
        {"revision": 0, "enabled": "true", "interval_seconds": 300},
        {"revision": False, "enabled": True, "interval_seconds": 300},
        {"revision": 0, "enabled": True, "interval_seconds": True},
    ],
)
def test_schedule_request_validation_is_strict(client, payload):
    assert (
        client.put(
            "/v1/sources/00000000-0000-0000-0000-000000000001/schedule", json=payload
        ).status_code
        == 422
    )


def test_migration_api_is_authenticated_explicit_and_revision_bound(
    client, tmp_path, monkeypatch
):
    from dataclasses import replace

    from openjarvis.connectors import source_adapters
    from openjarvis.connectors.source_adapters import ConfigMigration, get_adapter

    root = tmp_path / "documents"
    root.mkdir()
    item = client.post(
        "/v1/sources",
        json={
            "adapter_id": "local_files",
            "name": "Private",
            "config": {"path": str(root)},
        },
    ).json()
    monkeypatch.setitem(
        source_adapters._ADAPTERS,
        "local_files",
        replace(
            get_adapter("local_files"),
            config_version=2,
            migrations=(ConfigMigration(1, lambda cfg: cfg, preserves_index=True),),
        ),
    )
    base = f"/v1/sources/{item['id']}"
    for path, body in [
        (base + "/migration/preview", {"revision": 1}),
        (base + "/migration", {"revision": 1, "plan_token": "a" * 64}),
    ]:
        assert (
            client.post(path, json=body, headers={"Authorization": ""}).status_code
            == 401
        )
    assert client.get(base + "/audit", headers={"Authorization": ""}).status_code == 401
    assert (
        client.get("/v1/sources/audit", headers={"Authorization": ""}).status_code
        == 401
    )
    assert (
        client.post(base + "/migration/preview", json={"revision": True}).status_code
        == 422
    )
    assert (
        client.post(
            base + "/migration/preview", json={"revision": 1, "actor": "spoof"}
        ).status_code
        == 422
    )
    plan = client.post(base + "/migration/preview", json={"revision": 1}).json()
    assert not plan["index_reset"]
    assert len(client.get(base + "/audit").json()["events"]) == 1
    assert (
        client.post(
            base + "/migration", json={"revision": 1, "plan_token": "a" * 64}
        ).status_code
        == 409
    )
    result = client.post(
        base + "/migration", json={"revision": 1, "plan_token": plan["plan_token"]}
    )
    assert result.status_code == 200 and result.json()["config_version"] == 2
    assert (
        client.post(
            base + "/migration", json={"revision": 1, "plan_token": plan["plan_token"]}
        ).status_code
        == 409
    )
    event = client.get(base + "/audit").json()["events"][0]
    assert event["action"] == "migrated" and event["actor"] == "server_access"
    assert "Private" not in str(event) and str(root) not in str(event)
    assert client.delete(base + "?revision=2").status_code == 204
    assert client.get("/v1/sources/audit").json()["events"][0]["action"] == "removed"
    assert len(client.get(base + "/audit").json()["events"]) == 3
    assert client.get(base + "/audit?before_id=0").status_code == 422


def test_configuration_audit_attributes_verified_session_identity(client, tmp_path):
    from openjarvis.server.auth_store import AuthStore

    store = AuthStore(tmp_path / "sessions.db")
    store.create_user("owner", "Owner", "test-password-long")
    token = store.create_session("owner")
    client.app.state.auth_store = store
    root = tmp_path / "documents"
    root.mkdir()
    response = client.post(
        "/v1/sources",
        headers={"X-OpenJarvis-Session": token},
        json={
            "adapter_id": "local_files",
            "name": "Private",
            "config": {"path": str(root)},
        },
    )
    assert response.status_code == 201
    event = client.get("/v1/sources/audit").json()["events"][0]
    assert event["actor"] == "user:owner"
    assert token not in str(event)
