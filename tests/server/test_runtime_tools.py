"""Web installation requires a current administrator session and reviewed revision."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.server.auth_middleware import AuthMiddleware
from openjarvis.server.auth_store import AuthStore
from openjarvis.server.runtime_tools_router import create_runtime_tools_router
from openjarvis.tools.runtime_manager import RuntimeToolManager


@pytest.fixture
def setup(tmp_path):
    app = FastAPI()
    app.state.auth_store = AuthStore(tmp_path / "auth.db")
    auth = app.state.auth_store
    for uid in ("admin", "user"):
        auth.create_user(uid, uid, "test-password")
    auth.set_admin("admin", True)
    manager = RuntimeToolManager(tmp_path / "tools.db")
    app.include_router(create_runtime_tools_router(manager))
    app.add_middleware(AuthMiddleware, api_key="master")
    headers = {
        uid: {"X-OpenJarvis-Session": auth.create_session(uid)}
        for uid in ("admin", "user")
    }
    return TestClient(app), headers, manager, auth


def body():
    return {"name": "custom_web", "description": "Uppercase text", "transform": "upper"}


def test_all_management_routes_require_current_admin(setup):
    client, headers, manager, auth = setup
    tool = client.post(
        "/v1/runtime-tools", headers=headers["admin"], json=body()
    ).json()
    path = f"/v1/runtime-tools/{tool['id']}"
    for h in ({}, headers["user"], {"Authorization": "Bearer master"}):
        for method, suffix, payload in [
            ("GET", "", None),
            ("POST", "", body()),
            ("PUT", f"/{tool['id']}", {"revision": 1, "definition": body()}),
            ("POST", f"/{tool['id']}/approve", {"revision": 1}),
            ("PUT", f"/{tool['id']}/enabled", {"revision": 1, "enabled": True}),
            ("DELETE", f"/{tool['id']}", {"revision": 1}),
            ("GET", f"/{tool['id']}/audit", None),
        ]:
            result = client.request(
                method, "/v1/runtime-tools" + suffix, headers=h, json=payload
            )
            assert result.status_code in {401, 403}, result.text
    auth.set_admin("admin", False)
    assert (
        client.post(
            path + "/approve", headers=headers["admin"], json={"revision": 1}
        ).status_code
        == 403
    )
    assert manager.available() == []


def test_review_edit_disable_delete_and_audit(setup):
    client, headers, manager, _ = setup
    client.headers.update(headers["admin"])
    result = client.post("/v1/runtime-tools", json=body())
    assert result.status_code == 201, result.text
    row = result.json()
    assert not row["approved"]
    path = f"/v1/runtime-tools/{row['id']}"
    assert (
        client.put(path + "/enabled", json={"revision": 1, "enabled": True}).status_code
        == 400
    )
    row = client.post(path + "/approve", json={"revision": 1}).json()
    assert row["approved"] and row["enabled"]
    assert row["approved_by"] == "user:admin"
    digest = row["fingerprint"]
    row = client.put(
        path,
        json={
            "revision": row["revision"],
            "definition": {**body(), "transform": "lower"},
        },
    ).json()
    assert not row["approved"] and not row["enabled"]
    assert row["fingerprint"] != digest
    assert client.post(path + "/approve", json={"revision": 2}).status_code == 409
    row = client.post(path + "/approve", json={"revision": row["revision"]}).json()
    row = client.put(
        path + "/enabled", json={"revision": row["revision"], "enabled": False}
    ).json()
    assert manager.available() == []
    row = client.put(
        path + "/enabled", json={"revision": row["revision"], "enabled": True}
    ).json()
    assert (
        client.request("DELETE", path, json={"revision": row["revision"]}).status_code
        == 200
    )
    assert client.get("/v1/runtime-tools").json()["tools"] == []
    events = client.get(path + "/audit").json()["events"]
    assert events[0]["event"] == "deleted"
    assert all(e["actor"] == "user:admin" for e in events)


@pytest.mark.parametrize(
    "patch,status",
    [
        ({"transform": "shell"}, 400),
        ({"command": "echo bad"}, 422),
        ({"name": "web_search"}, 422),
        ({"approved": True}, 422),
    ],
)
def test_invalid_definitions_are_rejected(setup, patch, status):
    client, headers, manager, _ = setup
    result = client.post(
        "/v1/runtime-tools", headers=headers["admin"], json={**body(), **patch}
    )
    assert result.status_code == status
    assert manager.store.list() == []


def test_dynamic_adapter_metadata_and_formula_review(setup):
    from openjarvis.core.types import ToolCall
    from openjarvis.tools._stubs import ToolExecutor

    client, headers, manager, _ = setup
    client.headers.update(headers["admin"])
    adapters = client.get("/v1/runtime-tools").json()["adapters"]
    formula = next(a for a in adapters if a["adapter_id"] == "numeric_formula")
    assert [f["name"] for f in formula["fields"]] == ["expression", "variables"]
    definition = {
        "name": "custom_temp",
        "description": "Convert Celsius to Fahrenheit",
        "adapter_id": formula["adapter_id"],
        "config": formula["default_config"],
    }
    response = client.post("/v1/runtime-tools", json=definition)
    assert response.status_code == 201, response.text
    row = response.json()
    assert row["config"] == definition["config"]
    assert not row["approved"]
    path = f"/v1/runtime-tools/{row['id']}"
    row = client.post(path + "/approve", json={"revision": 1}).json()
    result = ToolExecutor(manager.available()).execute(
        ToolCall(id="formula", name="custom_temp", arguments='{"value":100}')
    )
    assert result.success and result.content == "212.0"
    assert (
        client.post(
            "/v1/runtime-tools", json={**definition, "transform": "upper"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/v1/runtime-tools", json={**definition, "adapter_id": "shell"}
        ).status_code
        == 400
    )
    edit = {**definition, "config": {"expression": "value * 2", "variables": ["value"]}}
    edited = client.put(
        path, json={"revision": row["revision"], "definition": edit}
    ).json()
    assert not edited["approved"]
    assert (
        client.post(path + "/approve", json={"revision": row["revision"]}).status_code
        == 409
    )


@pytest.mark.parametrize("stream", [False, True])
def test_chat_loads_new_tools_and_honors_revocation_without_restart(tmp_path, stream):
    from unittest.mock import MagicMock

    from openjarvis.agents._stubs import AgentResult, ToolUsingAgent
    from openjarvis.core.config import JarvisConfig
    from openjarvis.core.types import ToolCall
    from openjarvis.server.app import create_app

    class ProbeAgent(ToolUsingAgent):
        agent_id = "runtime_probe"

        def run(self, input, context=None, **kwargs):
            result = self._executor.execute(
                ToolCall(id="text", name="custom_web", arguments='{"input":"Hello"}')
            )
            return AgentResult(content=result.content, tool_results=[result], turns=1)

    config = JarvisConfig()
    config.analytics.enabled = False
    config.traces.enabled = False
    config.security.runtime_tools_db_path = str(tmp_path / "runtime.db")
    engine = MagicMock()

    async def no_tools_stream(*args, **kwargs):
        yield "No tools enabled"

    engine.stream = no_tools_stream
    agent = ProbeAgent(engine, "test", temperature=0.1, max_tokens=100)
    app = create_app(engine, "test", agent=agent, config=config)
    auth = app.state.auth_store
    for user in ("admin", "consumer"):
        auth.create_user(user, user, "test-password")
    auth.set_admin("admin", True)
    admin = {"X-OpenJarvis-Session": auth.create_session("admin")}
    consumer = {"X-OpenJarvis-Session": auth.create_session("consumer")}
    client = TestClient(app)
    row = client.post("/v1/runtime-tools", headers=admin, json=body()).json()
    path = f"/v1/runtime-tools/{row['id']}"
    row = client.post(path + "/approve", headers=admin, json={"revision": 1}).json()
    request = {
        "model": "test",
        "messages": [{"role": "user", "content": "Uppercase hello"}],
        "stream": stream,
    }
    result = client.post("/v1/chat/completions", headers=consumer, json=request)
    assert result.status_code == 200, result.text
    assert "HELLO" in result.text
    assert agent._tools == []
    assert agent._executor._tools == {}
    assert (
        client.put(
            path + "/enabled",
            headers=admin,
            json={"revision": row["revision"], "enabled": False},
        ).status_code
        == 200
    )
    result = client.post("/v1/chat/completions", headers=consumer, json=request)
    assert result.status_code == 200
    assert "HELLO" not in result.text
    assert ("No tools enabled" if stream else "Unknown tool") in result.text
