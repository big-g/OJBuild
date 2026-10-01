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
