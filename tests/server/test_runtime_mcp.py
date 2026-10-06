"""Administrator-only remote tool configuration, with secret-safe responses."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.mcp.runtime_manager import RuntimeMCPManager
from openjarvis.server.auth_middleware import AuthMiddleware
from openjarvis.server.auth_store import AuthStore
from openjarvis.server.runtime_mcp_router import create_runtime_mcp_router
from tests.mcp.test_runtime_connections import remote as remote


@pytest.fixture
def setup(tmp_path, remote):
    app = FastAPI()
    auth = AuthStore(tmp_path / "auth.db")
    for uid in ("admin", "user"):
        auth.create_user(uid, uid, "test-password")
    auth.set_admin("admin", True)
    app.state.auth_store = auth
    manager = RuntimeMCPManager(tmp_path / "mcp.db")
    app.include_router(create_runtime_mcp_router(manager))
    app.add_middleware(AuthMiddleware, api_key="master")
    headers = {
        uid: {"X-OpenJarvis-Session": auth.create_session(uid)}
        for uid in ("admin", "user")
    }
    return TestClient(app), manager, headers, auth, remote


def body(**extra):
    return {
        "name": "example",
        "url": "https://example.com/mcp",
        "allow_without_confirmation": True,
        "bearer_token": "TEST-SECRET",
    } | extra


def test_api_key_web_save_review_and_blank_edit_retention(setup):
    client, manager, headers, _, remote = setup
    client.headers.update(headers["admin"])
    payload = body(
        auth_type="api_key", api_key_header="X-API-Key", credential_secret="TEST-SECRET"
    )
    del payload["bearer_token"]
    result = client.post("/v1/runtime-mcp", json=payload)
    assert result.status_code == 201, result.text
    saved = result.json()
    assert saved["auth_type"] == "api_key" and saved["api_key_header"] == "x-api-key"
    assert saved["has_token"] and "TEST-SECRET" not in result.text
    assert "credential_secret" not in saved and not remote["clients"]
    path = f"/v1/runtime-mcp/{saved['id']}"
    saved = client.post(path + "/discover", json={"revision": saved["revision"]}).json()
    saved = client.post(path + "/approve", json={"revision": saved["revision"]}).json()
    assert saved["approved"]
    del payload["credential_secret"]
    saved = client.put(
        path, json={"revision": saved["revision"], "definition": payload}
    ).json()
    assert saved["has_token"] and not saved["approved"]
    assert manager.store.token(manager.store.get(saved["id"])) == "TEST-SECRET"
    payload["credential_secret"] = ""
    saved = client.put(
        path, json={"revision": saved["revision"], "definition": payload}
    ).json()
    assert not saved["has_token"]


@pytest.mark.parametrize(
    "patch,status",
    [
        ({"api_key_header": "Host"}, 400),
        ({"api_key_header": "bad header"}, 400),
        ({"api_key_header": ""}, 400),
        ({"credential_secret": "bad secret"}, 400),
        ({"auth_type": "oauth"}, 422),
        ({"bearer_token": "ANOTHER-SECRET"}, 400),
    ],
)
def test_invalid_api_key_requests_never_echo_secrets(setup, patch, status):
    client, manager, headers, _, remote = setup
    payload = {
        "name": "example",
        "url": "https://example.com/mcp",
        "auth_type": "api_key",
        "api_key_header": "X-API-Key",
        "credential_secret": "TEST-SECRET",
    } | patch
    result = client.post("/v1/runtime-mcp", headers=headers["admin"], json=payload)
    assert result.status_code == status
    assert "TEST-SECRET" not in result.text and "ANOTHER-SECRET" not in result.text
    assert not manager.store.list() and not remote["clients"]


def test_all_management_routes_require_current_admin(setup):
    client, manager, headers, auth, remote = setup
    row = client.post("/v1/runtime-mcp", headers=headers["admin"], json=body()).json()
    for header in ({}, headers["user"], {"Authorization": "Bearer master"}):
        for method, suffix, payload in [
            ("GET", "", None),
            ("POST", "", body()),
            ("PUT", f"/{row['id']}", {"revision": 1, "definition": body()}),
            ("POST", f"/{row['id']}/discover", {"revision": 1}),
            ("POST", f"/{row['id']}/approve", {"revision": 1}),
            ("PUT", f"/{row['id']}/enabled", {"revision": 1, "enabled": True}),
            ("DELETE", f"/{row['id']}", {"revision": 1}),
            ("GET", f"/{row['id']}/audit", None),
        ]:
            result = client.request(
                method, "/v1/runtime-mcp" + suffix, headers=header, json=payload
            )
            assert result.status_code in {401, 403}, result.text
            assert "TEST-SECRET" not in result.text
    assert remote["clients"] == []
    auth.set_admin("admin", False)
    assert client.get("/v1/runtime-mcp", headers=headers["admin"]).status_code == 403
    assert manager.available() == []


def test_web_review_revocation_and_secret_free_audit(setup):
    client, manager, headers, _, remote = setup
    client.headers.update(headers["admin"])
    response = client.post("/v1/runtime-mcp", json=body())
    assert response.status_code == 201 and "TEST-SECRET" not in response.text
    row = response.json()
    path = f"/v1/runtime-mcp/{row['id']}"
    assert row["has_token"] and not row["approved"]
    assert client.post(path + "/approve", json={"revision": 1}).status_code == 400
    row = client.post(path + "/discover", json={"revision": 1}).json()
    assert row["tools"][0]["remote_name"] == "echo"
    assert row["tools"][0]["parameters"]["required"] == ["input"]
    assert not row["approved"] and remote["calls"] == 0
    assert client.post(path + "/approve", json={"revision": 1}).status_code == 409
    row = client.post(path + "/approve", json={"revision": row["revision"]}).json()
    assert row["approved"] and row["approved_by"] == "user:admin"
    row = client.put(
        path + "/enabled", json={"revision": row["revision"], "enabled": False}
    ).json()
    assert manager.available() == []
    row = client.put(
        path + "/enabled", json={"revision": row["revision"], "enabled": True}
    ).json()
    row = client.put(
        path,
        json={"revision": row["revision"], "definition": body(bearer_token="ROTATED")},
    ).json()
    assert not row["enabled"] and not row["approved"] and not row["discovered"]
    assert (
        client.put(
            path + "/enabled", json={"revision": row["revision"], "enabled": True}
        ).status_code
        == 400
    )
    assert (
        client.request("DELETE", path, json={"revision": row["revision"]}).status_code
        == 200
    )
    assert client.get("/v1/runtime-mcp").json() == {"connections": []}
    response = client.get(path + "/audit")
    assert "TEST-SECRET" not in response.text and "ROTATED" not in response.text
    assert response.json()["events"][0]["event"] == "deleted"


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"command": "TEST-SECRET"}, 422),
        ({"bearer_token": ["TEST-SECRET"]}, 422),
        ({"allow_without_confirmation": "TEST-SECRET"}, 422),
        ({"name": "TEST-SECRET"}, 422),
        ({"bearer_token": "spaces not allowed"}, 400),
        ({"url": "http://example.com/mcp"}, 400),
    ],
)
def test_invalid_requests_do_not_echo_tokens(setup, changes, status):
    client, manager, headers, _, remote = setup
    response = client.post(
        "/v1/runtime-mcp", headers=headers["admin"], json=body(**changes)
    )
    assert response.status_code == status, response.text
    assert "TEST-SECRET" not in response.text
    assert not manager.store.list() and not remote["clients"]


@pytest.mark.parametrize("name", ["Home Tools", "home-tools", "2home", "a" * 25, ""])
def test_invalid_name_returns_actionable_secret_safe_guidance(setup, name):
    client, manager, headers, _, remote = setup
    response = client.post(
        "/v1/runtime-mcp",
        headers=headers["admin"],
        json=body(name=name, bearer_token="TEST-SECRET"),
    )
    assert response.status_code == 422
    assert "lowercase letter" in response.json()["detail"]
    assert "underscores" in response.json()["detail"]
    assert "home_tools" in response.json()["detail"]
    assert "TEST-SECRET" not in response.text
    assert not manager.store.list() and not remote["clients"]


def test_edit_name_error_has_same_guidance_and_keeps_saved_connection(setup):
    client, manager, headers, _, _ = setup
    response = client.post("/v1/runtime-mcp", headers=headers["admin"], json=body())
    saved = response.json()
    response = client.put(
        f"/v1/runtime-mcp/{saved['id']}",
        headers=headers["admin"],
        json={
            "revision": saved["revision"],
            "definition": body(name="Invalid Name", bearer_token="TEST-SECRET"),
        },
    )
    assert response.status_code == 422
    assert (
        "home_tools" in response.json()["detail"] and "TEST-SECRET" not in response.text
    )
    assert manager.store.get(saved["id"])["revision"] == saved["revision"]
