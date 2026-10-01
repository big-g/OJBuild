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
    assert definitions[0]["fields"][0]["name"] == "path"
    assert definitions[0]["required_capabilities"] == [
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
