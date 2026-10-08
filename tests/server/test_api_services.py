"""Session-authenticated service configuration and detached templates."""

import json
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.connectors.source_manager import SourceManager
from openjarvis.connectors.source_store import SourceStore
from openjarvis.server.auth_middleware import AuthMiddleware
from openjarvis.server.auth_store import AuthStore
from openjarvis.server.sources_router import create_sources_router

DEFINITION = {
    "version": 1,
    "base_url": "https://api.example.com",
    "operations": [
        {"id": "readings", "endpoint": "/readings", "response": {"mode": "document"}}
    ],
}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    manager = SourceManager(
        SourceStore(str(tmp_path / "sources.db"), legacy_path=str(tmp_path / "none")),
        knowledge_path=str(tmp_path / "knowledge.db"),
    )
    auth = AuthStore(tmp_path / "auth.db")
    headers = {}
    for uid in ("alice", "bob"):
        auth.create_user(uid, uid, "test-password")
        headers[uid] = {"X-OpenJarvis-Session": auth.create_session(uid)}
    app = FastAPI()
    app.state.auth_store = auth
    app.add_middleware(AuthMiddleware, api_key="master")
    app.include_router(create_sources_router(manager))
    fetch = MagicMock(
        return_value=httpx.Response(
            200,
            json={"temperature": 72},
            request=httpx.Request("GET", "https://api.example.com/readings"),
        )
    )
    monkeypatch.setattr("openjarvis.connectors.api_service.fetch_public_source", fetch)
    with TestClient(app, base_url="https://testserver") as client:
        yield client, headers, manager, fetch


def test_template_routes_require_session_and_enforce_owner_and_revision(setup):
    client, headers, _, fetch = setup
    path = "/v1/sources/api-templates"
    assert client.get(path).status_code == 401
    body = {"name": "Readings", "definition": DEFINITION}
    result = client.post(path, headers=headers["alice"], json=body)
    assert result.status_code == 201, result.text
    saved = result.json()
    assert client.get(path, headers=headers["bob"]).json() == {"templates": []}
    identity = path + "/" + saved["id"]
    assert (
        client.put(
            identity, headers=headers["bob"], json={**body, "revision": 1}
        ).status_code
        == 409
    )
    assert (
        client.delete(identity + "?revision=1", headers=headers["bob"]).status_code
        == 409
    )
    assert (
        client.put(
            identity, headers=headers["alice"], json={**body, "revision": 1}
        ).json()["revision"]
        == 2
    )
    assert (
        client.delete(identity + "?revision=1", headers=headers["alice"]).status_code
        == 409
    )
    assert (
        client.delete(identity + "?revision=2", headers=headers["alice"]).status_code
        == 204
    )
    fetch.assert_not_called()


def test_api_source_test_create_and_isolation_reuse_existing_source_path(setup):
    client, headers, manager, fetch = setup
    config = {"definition": json.dumps(DEFINITION)}
    tested = client.post(
        "/v1/sources/test",
        headers=headers["alice"],
        json={"adapter_id": "api_service", "config": config},
    )
    assert tested.status_code == 200, tested.text
    assert (
        tested.json()["documents"] == 1
        and tested.json()["request_trace"][0]["status"] == 200
    )
    assert manager.list() == []
    created = client.post(
        "/v1/sources",
        headers=headers["alice"],
        json={"adapter_id": "api_service", "name": "Readings", "config": config},
    )
    assert created.status_code == 201, created.text
    saved = created.json()
    assert saved["owner_id"] == "alice" and saved["sharing"] == "personal"
    assert client.get("/v1/sources", headers=headers["bob"]).json()["sources"] == []
    assert manager.sync(saved["id"]) > 0
    assert manager.list()[0]["chunks"] > 0


@pytest.mark.parametrize(
    "text",
    [
        '{"openapi":"3.1.0","servers":[null]}',
        '{"version":1,"base_url":"https://api.example.com","operations":[null]}',
        'curl https://api.example.com/data -H "Authorization: Bearer secret"',
    ],
)
def test_bad_imports_fail_as_validation_not_server_errors(setup, text):
    client, headers, _, fetch = setup
    result = client.post(
        "/v1/sources/api-import", headers=headers["alice"], json={"text": text}
    )
    assert result.status_code == 400, result.text
    assert "Bearer secret" not in result.text
    fetch.assert_not_called()
