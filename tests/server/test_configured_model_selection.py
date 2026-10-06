"""Two Ollama servers, live capability gates, streams and shared-agent isolation."""

import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from openjarvis.agents._stubs import AgentResult
from openjarvis.core.config import JarvisConfig
from openjarvis.core.events import EventBus
from openjarvis.core.types import Message, Role
from openjarvis.engine._base import EngineConnectionError, InferenceEngine
from openjarvis.engine.configured_models import (
    ConfiguredModelEngine,
    model_id,
    preserve_wrappers,
    selectable_models,
)
from openjarvis.security.guardrails import GuardrailsEngine
from openjarvis.server.app import create_app
from openjarvis.server.auth_store import AuthStore
from openjarvis.server.research_router import _build_planner_engine
from openjarvis.telemetry.instrumented_engine import InstrumentedEngine


class DefaultEngine(InferenceEngine):
    engine_id = "ollama"

    def __init__(self):
        self.calls = []

    def generate(self, messages, *, model, **kwargs):
        self.calls.append(model)
        return {"content": "default engine", "model": model}

    async def stream(self, messages, *, model, **kwargs):
        self.calls.append(model)
        yield "default engine"

    def list_models(self):
        return ["default:latest"]

    def health(self):
        return True


@pytest.fixture
def setup(tmp_path, monkeypatch):
    config = JarvisConfig()
    config.security.runtime_tools_db_path = str(tmp_path / "tools.db")
    config.security.model_connections_db_path = str(tmp_path / "models.db")
    config.security.generated_files_dir = str(tmp_path / "files")
    default = DefaultEngine()
    app = create_app(default, "default:latest", config=config, engine_name="ollama")
    auth = AuthStore(tmp_path / "auth.db")
    auth.create_user("admin", "admin", "test-password", is_admin=True)
    auth.create_user("user", "user", "test-password")
    app.state.auth_store = auth
    headers = {
        uid: {"X-OpenJarvis-Session": auth.create_session(uid)}
        for uid in ("admin", "user")
    }
    client = TestClient(app, headers=headers["admin"])
    requests, options = [], []
    state = {
        "caps": ["completion", "tools", "vision"],
        "down": False,
        "reject_tools": False,
        "finish": "stop",
    }

    def handler(request):
        requests.append(request)
        if state["down"]:
            raise httpx.ConnectError("private provider details", request=request)
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": "qwen3.5:9b", "size": 6600000000}]}
            )
        body = json.loads(request.content)
        assert body["model"] == "qwen3.5:9b", (
            "Serving identity must not be the UI alias"
        )
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": state["caps"]})
        assert request.url.path == "/api/chat", (
            "No preloads, pulls or alternate endpoints"
        )
        if state["reject_tools"]:
            assert body.get("tools"), "A failed tool request must not retry tools-less"
            return httpx.Response(400, json={"error": "PRIVATE_PROVIDER_DETAILS"})
        content = f"{request.url.host}:qwen3.5:9b"
        if body.get("stream"):
            return httpx.Response(
                200,
                text="\n".join(
                    [
                        json.dumps({"message": {"content": content}}),
                        json.dumps(
                            {
                                "done": True,
                                "done_reason": state["finish"],
                                "eval_count": 2,
                            }
                        ),
                    ]
                ),
            )
        return httpx.Response(
            200,
            json={
                "message": {"content": content},
                "done": True,
                "done_reason": "stop",
                "eval_count": 2,
            },
        )

    sync, asynchronous = httpx.Client, httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def sync_factory(**kwargs):
        options.append(kwargs)
        return sync(transport=transport, **kwargs)

    def async_factory(**kwargs):
        options.append(kwargs)
        kwargs.pop("transport", None)
        return asynchronous(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "Client", sync_factory)
    monkeypatch.setattr(httpx, "AsyncClient", async_factory)
    return app, client, headers, requests, state, default, options


def enabled(setup, name="first", host="127.0.0.1"):
    _, client, _, _, _, _, _ = setup
    row = client.post(
        "/v1/model-connections", json={"name": name, "url": f"http://{host}:11434"}
    ).json()
    path = f"/v1/model-connections/{row['id']}"
    result = client.post(path + "/test", json={"revision": row["revision"]})
    assert result.json()["ok"], result.text
    row = result.json()["connection"]
    result = client.post(
        path + "/capabilities",
        json={"revision": row["revision"], "serving_id": "qwen3.5:9b"},
    )
    assert result.json()["ok"], result.text
    row = result.json()["connection"]
    result = client.post(
        path + "/enabled", json={"revision": row["revision"], "enabled": True}
    )
    assert result.status_code == 200, result.text
    return result.json()


def chat(client, selected, **kwargs):
    return client.post(
        "/v1/chat/completions",
        json={
            "model": selected,
            "messages": [{"role": "user", "content": "Say hello"}],
            **kwargs,
        },
    )


@pytest.mark.parametrize("stream", [False, True])
def test_two_identical_models_route_only_to_the_selected_server(setup, stream):
    app, client, headers, requests, _, default, options = setup
    a = enabled(setup)
    b = enabled(setup, "second", "192.168.1.20")
    client.headers.update(headers["user"])
    models = client.get("/v1/models").json()["data"]
    remote = [m for m in models if m["owned_by"] == "configured_ollama"]
    assert len(remote) == 2 and remote[0]["id"] != remote[1]["id"]
    assert {m["display_name"] for m in remote} == {
        "qwen3.5:9b — first",
        "qwen3.5:9b — second",
    }
    assert all("url" not in m and m["capability_state"] == "reported" for m in remote)
    for row, host in [(a, "127.0.0.1"), (b, "192.168.1.20")]:
        selected = model_id(row["id"], "qwen3.5:9b")
        result = chat(client, selected, stream=stream)
        assert result.status_code == 200, result.text
        assert f"{host}:qwen3.5:9b" in result.text
        assert selected in result.text
    assert not default.calls
    assert app.state.engine is default
    assert all(kw.get("trust_env") is False for kw in options)
    assert len([r for r in requests if r.url.path == "/api/chat"]) == 2


def test_enable_requires_capability_read_and_regular_users_cannot_enable(setup):
    _, client, headers, _, _, _, _ = setup
    row = client.post(
        "/v1/model-connections", json={"name": "gpu", "url": "http://localhost:11434"}
    ).json()
    path = f"/v1/model-connections/{row['id']}"
    assert not row["enabled"]
    assert (
        client.post(
            path + "/enabled", json={"revision": 1, "enabled": True}
        ).status_code
        == 400
    )
    for action, body in [
        ("enabled", {"revision": 1, "enabled": True}),
        ("capabilities", {"revision": 1, "serving_id": "qwen3.5:9b"}),
    ]:
        assert (
            client.post(
                path + "/" + action, headers=headers["user"], json=body
            ).status_code
            == 403
        )


@pytest.mark.parametrize("change", ["disabled", "edited", "removed", "catalog"])
def test_live_configuration_changes_block_selected_alias_without_fallback(
    setup, change
):
    app, client, _, requests, _, default, _ = setup
    row = enabled(setup)
    selected = model_id(row["id"], "qwen3.5:9b")
    bound = ConfiguredModelEngine(app.state.model_connection_store, selected)
    store = app.state.model_connection_store
    if change == "disabled":
        store.enable(row["id"], row["revision"], False, "admin")
    elif change == "edited":
        store.update(
            row["id"], row["revision"], "edited", "http://192.168.1.20:11434", "admin"
        )
    elif change == "removed":
        store.remove(row["id"], row["revision"], "admin")
    else:
        store.discovered(row["id"], row["revision"], row["catalog"], True, "admin")
    assert not selectable_models(store)
    requests.clear()
    with pytest.raises(EngineConnectionError, match="changed|removed"):
        bound.generate([Message(role=Role.USER, content="hi")], model=selected)
    assert not requests and not default.calls
    assert chat(client, selected).status_code == 400


@pytest.mark.parametrize(
    "cap,payload",
    [
        ("tools", {"tools": [{"type": "function", "function": {"name": "lookup"}}]}),
        (
            "vision",
            {"messages": [{"role": "user", "content": "hi", "images": ["fake"]}]},
        ),
    ],
)
def test_missing_reported_capability_rejected_before_model_execution(
    setup, cap, payload
):
    _, client, _, requests, state, default, _ = setup
    state["caps"].remove(cap)
    row = enabled(setup)
    requests.clear()
    result = chat(client, model_id(row["id"], "qwen3.5:9b"), **payload)
    assert result.status_code == 400
    assert "support" in result.text
    assert not requests and not default.calls


def test_live_capability_downgrade_and_outage_surface_without_execution(setup):
    _, client, _, requests, state, default, _ = setup
    row = enabled(setup)
    selected = model_id(row["id"], "qwen3.5:9b")
    state["caps"] = ["completion"]
    result = chat(
        client, selected, tools=[{"type": "function", "function": {"name": "lookup"}}]
    )
    assert result.status_code == 503 and "tool-calling support" in result.text
    state["down"] = True
    result = chat(client, selected)
    assert result.status_code == 503 and "unavailable" in result.text
    assert "private provider details" not in result.text
    assert not any(r.url.path == "/api/chat" for r in requests) and not default.calls


@pytest.mark.parametrize("stream", [False, True])
def test_provider_tool_rejection_never_retries_without_tools(setup, stream):
    _, client, _, requests, state, default, _ = setup
    row = enabled(setup)
    state["reject_tools"] = True
    result = chat(
        client,
        model_id(row["id"], "qwen3.5:9b"),
        stream=stream,
        tools=[
            {
                "type": "function",
                "function": {"name": "lookup", "parameters": {"type": "object"}},
            }
        ],
    )
    assert result.status_code == (200 if stream else 502)
    assert len([r for r in requests if r.url.path == "/api/chat"]) == 1
    assert "PRIVATE_PROVIDER_DETAILS" not in result.text
    assert not default.calls


def test_stream_finish_reason_and_evidence_gate_are_preserved(setup):
    _, client, _, requests, state, default, _ = setup
    row = enabled(setup)
    selected = model_id(row["id"], "qwen3.5:9b")
    state["finish"] = "length"
    result = chat(client, selected, stream=True)
    assert (
        '"finish_reason":"length"' in result.text
        or '"finish_reason": "length"' in result.text
    )
    requests.clear()
    result = chat(
        client,
        selected,
        messages=[
            {"role": "user", "content": "What is the current weather in Boston?"}
        ],
    )
    assert result.status_code == 200
    assert not any(r.url.path == "/api/chat" for r in requests)
    assert not default.calls


class Agent:
    accepts_tools = False

    def __init__(self, engine):
        self._engine, self._model, self._tools = engine, "default:latest", []

    def run(self, text, context=None):
        result = self._engine.generate(
            [Message(role=Role.USER, content=text)], model=self._model
        )
        return AgentResult(content=result["content"], turns=1)


@pytest.mark.parametrize("stream", [False, True])
def test_agent_engine_and_model_restored_after_success_and_failure(setup, stream):
    app, client, _, _, state, default, _ = setup
    row = enabled(setup)
    agent = Agent(default)
    if stream:
        agent._tools = [object()]
    app.state.agent = agent
    selected = model_id(row["id"], "qwen3.5:9b")
    assert "127.0.0.1:qwen3.5:9b" in chat(client, selected, stream=stream).text
    assert agent._engine is default and agent._model == "default:latest"
    state["reject_tools"] = True
    # Force an engine failure inside the locked agent run, after preflight.
    state["down"] = False
    original = agent.run

    def failed(*args, **kwargs):
        state["down"] = True
        return original(*args, **kwargs)

    agent.run = failed
    result = chat(client, selected, stream=stream)
    assert result.status_code in (200, 503)
    assert agent._engine is default and agent._model == "default:latest"
    assert not default.calls


def test_concurrent_agent_requests_keep_their_server_binding(setup):
    app, client, _, _, _, default, _ = setup
    a, b = enabled(setup), enabled(setup, "second", "192.168.1.20")
    app.state.agent = Agent(default)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda row: chat(client, model_id(row["id"], "qwen3.5:9b")), [a, b]
            )
        )
    assert "127.0.0.1:qwen3.5:9b" in results[0].text
    assert "192.168.1.20:qwen3.5:9b" in results[1].text
    assert (
        app.state.agent._engine is default
        and app.state.agent._model == "default:latest"
    )


def test_wrappers_and_research_planner_keep_safety_and_explicit_selection(setup):
    app, _, _, _, _, default, _ = setup
    row = enabled(setup)
    selected = model_id(row["id"], "qwen3.5:9b")
    original = InstrumentedEngine(GuardrailsEngine(default, scanners=[]), EventBus())
    bound = ConfiguredModelEngine(app.state.model_connection_store, selected)
    wrapped = preserve_wrappers(original, bound)
    assert isinstance(wrapped, InstrumentedEngine) and wrapped is not original
    assert (
        isinstance(wrapped._inner, GuardrailsEngine) and wrapped._inner._engine is bound
    )
    assert original._inner._engine is default
    key, planner, model = _build_planner_engine(
        JarvisConfig(),
        active_engine=original,
        active_engine_key="multi",
        request_model=selected,
        model_connection_store=app.state.model_connection_store,
    )
    assert key == "ollama" and model == selected
    assert (
        "127.0.0.1:qwen3.5:9b"
        in planner.generate([Message(role=Role.USER, content="hi")], model=model)[
            "content"
        ]
    )


def test_capability_failure_withdraws_enable_and_snapshot(setup):
    _, client, _, _, state, _, _ = setup
    row = enabled(setup)
    state["caps"] = [True]
    result = client.post(
        f"/v1/model-connections/{row['id']}/capabilities",
        json={"revision": row["revision"], "serving_id": "qwen3.5:9b"},
    )
    assert not result.json()["ok"]
    row = result.json()["connection"]
    assert not row["enabled"] and row["catalog"][0]["capability_state"] == "unknown"


@pytest.mark.parametrize("stream", [False, True])
def test_vision_selection_forwards_images_to_its_server(setup, stream):
    import base64

    _, client, _, requests, _, default, _ = setup
    row = enabled(setup)
    image = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    result = chat(
        client,
        model_id(row["id"], "qwen3.5:9b"),
        stream=stream,
        messages=[{"role": "user", "content": "Describe this", "images": [image]}],
    )
    assert result.status_code == 200 and "127.0.0.1:qwen3.5:9b" in result.text
    calls = [r for r in requests if r.url.path == "/api/chat"]
    assert json.loads(calls[0].content)["messages"][-1]["images"] == [image]
    assert not default.calls


def test_enabled_model_catalog_requires_a_human_session(setup):
    _, client, _, _, _, _, _ = setup
    enabled(setup)
    client.headers.pop("X-OpenJarvis-Session")
    assert client.get("/v1/models").status_code == 401


def test_non_chat_manifest_cannot_enable_connection(setup):
    _, client, _, _, state, _, _ = setup
    state["caps"] = ["embedding"]
    row = client.post(
        "/v1/model-connections",
        json={"name": "embedding", "url": "http://localhost:11434"},
    ).json()
    path = f"/v1/model-connections/{row['id']}"
    row = client.post(path + "/test", json={"revision": row["revision"]}).json()[
        "connection"
    ]
    row = client.post(
        path + "/capabilities",
        json={"revision": row["revision"], "serving_id": "qwen3.5:9b"},
    ).json()["connection"]
    result = client.post(
        path + "/enabled", json={"revision": row["revision"], "enabled": True}
    )
    assert result.status_code == 400
