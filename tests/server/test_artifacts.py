import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.artifacts.store import ArtifactStore
from openjarvis.server.artifacts_router import create_artifacts_router
from openjarvis.server.auth_middleware import AuthMiddleware
from openjarvis.server.auth_store import AuthStore


@pytest.fixture
def setup(tmp_path):
    app = FastAPI()
    app.state.auth_store = AuthStore(tmp_path / "auth.db")
    auth = app.state.auth_store
    headers = {}
    for uid in ("alice", "bob"):
        auth.create_user(uid, uid, "test-password")
        headers[uid] = {"X-OpenJarvis-Session": auth.create_session(uid)}
    auth.set_admin("bob", True)
    store = ArtifactStore(tmp_path / "artifacts")
    app.include_router(create_artifacts_router(store))
    app.add_middleware(AuthMiddleware, api_key="master")
    return TestClient(app), headers, store, auth


def test_owner_scope_all_routes_and_download_headers(setup):
    client, headers, store, auth = setup
    h = headers["alice"]
    response = client.post(
        "/v1/files",
        headers=h,
        json={"filename": "script ü.html", "content": "<script>alert(1)</script>"},
    )
    assert response.status_code == 201, response.text
    item = response.json()
    path = "/v1/files/" + item["id"]
    for suffix in ("/preview", "/download"):
        assert client.get(path + suffix, headers=headers["bob"]).status_code == 404
    assert client.delete(path, headers=headers["bob"]).status_code == 404
    assert client.get("/v1/files", headers=headers["bob"]).json()["files"] == []
    preview = client.get(path + "/preview", headers=h)
    assert preview.headers["content-type"] == "application/json"
    assert preview.json()["text"] == "<script>alert(1)</script>"
    download = client.get(path + "/download", headers=h)
    assert download.content == b"<script>alert(1)</script>"
    assert download.headers["content-type"] == "application/octet-stream"
    assert download.headers["content-disposition"].startswith("attachment;")
    assert "script%20%C3%BC.html" in download.headers["content-disposition"]
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "no-store"
    assert "sandbox" in download.headers["content-security-policy"]
    assert client.delete(path, headers=h).status_code == 204
    assert client.get(path + "/download", headers=h).status_code == 404


def test_missing_master_only_and_revoked_sessions_denied(setup):
    client, headers, store, auth = setup
    item = store.save("alice", "a.py", "test")
    path = "/v1/files/" + item["id"]
    auth.delete_user("alice")
    for h in ({}, {"Authorization": "Bearer master"}, headers["alice"]):
        for method, url, values in [
            ("GET", "/v1/files", None),
            ("POST", "/v1/files", {"filename": "x", "content": "x"}),
            ("GET", path + "/preview", None),
            ("GET", path + "/download", None),
            ("DELETE", path, None),
        ]:
            assert (
                client.request(method, url, headers=h, json=values).status_code == 401
            )


def test_bad_and_oversized_requests(setup, monkeypatch):
    client, headers, store, auth = setup
    for values in [
        {"filename": "../x", "content": "x"},
        {"filename": "a", "content": "x", "owner": "bob"},
        {"filename": "a", "content": 42},
        [],
        {"filename": "a", "content": "??", "encoding": "base64"},
    ]:
        assert (
            client.post("/v1/files", headers=headers["alice"], json=values).status_code
            == 400
        )
    assert (
        client.post(
            "/v1/files", headers=headers["alice"], content="not-json"
        ).status_code
        == 400
    )
    from openjarvis.server import artifacts_router

    monkeypatch.setattr(artifacts_router, "MAX_REQUEST_BYTES", 8)
    assert (
        client.post(
            "/v1/files", headers=headers["alice"], content="123456789"
        ).status_code
        == 413
    )


@pytest.mark.parametrize("stream", [False, True])
def test_chat_tool_saves_as_authenticated_user_and_restores_binding(tmp_path, stream):
    from unittest.mock import MagicMock

    from openjarvis.agents._stubs import AgentResult, ToolUsingAgent
    from openjarvis.core.config import JarvisConfig
    from openjarvis.core.types import ToolCall
    from openjarvis.server.app import create_app

    class SaveAgent(ToolUsingAgent):
        agent_id = "file_probe"

        def run(self, input, context=None, **kwargs):
            result = self._executor.execute(
                ToolCall(
                    id="file",
                    name="artifact_save",
                    arguments='{"filename":"generated.py","content":"print(42)"}',
                )
            )
            assert result.success, result.content
            return AgentResult(content=result.content, tool_results=[result], turns=1)

    config = JarvisConfig()
    config.analytics.enabled = False
    config.traces.enabled = False
    config.security.runtime_tools_db_path = str(tmp_path / "runtime.db")
    config.security.generated_files_dir = str(tmp_path / "private")
    agent = SaveAgent(MagicMock(), "test")
    app = create_app(agent._engine, "test", agent=agent, config=config)
    auth = app.state.auth_store
    client = TestClient(app)
    sessions = {}
    for uid in ("alice", "bob"):
        auth.create_user(uid, uid, "test-password")
        sessions[uid] = {"X-OpenJarvis-Session": auth.create_session(uid)}
    response = client.post(
        "/v1/chat/completions",
        headers=sessions["alice"],
        json={
            "model": "test",
            "messages": [{"role": "user", "content": "save a Python file"}],
            "stream": stream,
        },
    )
    assert response.status_code == 200, response.text
    assert "generated.py" in response.text
    assert agent._tools == [] and agent._executor._tools == {}
    own = client.get("/v1/files", headers=sessions["alice"]).json()["files"]
    assert len(own) == 1 and own[0]["filename"] == "generated.py"
    assert client.get("/v1/files", headers=sessions["bob"]).json()["files"] == []
    assert (
        client.get(
            "/v1/files/" + own[0]["id"] + "/download", headers=sessions["alice"]
        ).content
        == b"print(42)"
    )
