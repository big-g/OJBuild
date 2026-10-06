"""Explicit task assignments, objective probes, revision gates and ownership."""

import json
import sqlite3

import httpx
import pytest

from openjarvis.core.types import Message, Role
from openjarvis.engine._base import EngineConnectionError
from openjarvis.engine.configured_models import model_id
from openjarvis.engine.connection_store import ModelConnectionStore
from openjarvis.engine.model_benchmarks import CASES, _answer_ok, _tool_ok
from openjarvis.engine.task_routing import TaskRoutingStore
from tests.server.test_configured_model_selection import (  # noqa: F401
    chat,
    enabled,
    setup,
)


@pytest.fixture
def probes(setup, monkeypatch):  # noqa: F811
    app, client, headers, requests, state, default, _ = setup
    settings = {"wrong": False, "tools_wrong": False, "mutate": None}
    original = httpx.Client

    def handle(request):
        requests.append(request)
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": state["caps"]})
        body = json.loads(request.content)
        if settings["mutate"]:
            settings["mutate"]()
            settings["mutate"] = None
        if body.get("tools"):
            calls = (
                []
                if settings["tools_wrong"]
                else [
                    {
                        "function": {
                            "name": "diagnostic_echo",
                            "arguments": {"marker": "oj_probe_v1"},
                        }
                    }
                ]
            )
            message = {"content": "", "tool_calls": calls}
        else:
            content = body["messages"][0]["content"]
            matched = next(
                (
                    (task, expected)
                    for task, cases in CASES.items()
                    for _, prompt, expected in cases
                    if prompt == content
                ),
                None,
            )
            task = matched[0] if matched else None
            if task is None:
                return httpx.Response(
                    200,
                    json={
                        "message": {"content": f"{request.url.host}:qwen3.5:9b"},
                        "done_reason": "stop",
                    },
                )
            answer = "wrong" if settings["wrong"] else matched[1]
            if task == "vision":
                assert body["messages"][0]["images"][0].startswith("iVBOR")
            message = {"content": json.dumps({"answer": answer})}
        assert body["options"]["num_predict"] <= 512
        assert body["think"] is False
        return httpx.Response(
            200,
            json={
                "message": message,
                "done_reason": "stop",
                "prompt_eval_count": 12,
                "eval_count": 3,
            },
        )

    def activate_transport():
        # The fixture's original transport still serves catalog/regular chat.
        def factory(**kwargs):
            bound = original(**kwargs)
            bound._transport = httpx.MockTransport(handle)
            return bound

        monkeypatch.setattr(httpx, "Client", factory)

    return setup, settings, activate_transport


def diagnostic(client, row, task="coding"):
    return client.post(
        "/v1/model-routing/benchmarks",
        json={
            "task": task,
            "model_id": model_id(row["id"], "qwen3.5:9b"),
            "connection_revision": row["revision"],
        },
    )


def assign(client, bench, **overrides):
    rule = next(
        r
        for r in client.get("/v1/model-routing").json()["rules"]
        if r["task"] == bench["task"]
    )
    return client.put(
        f"/v1/model-routing/tasks/{rule['task']}",
        json={
            "revision": rule["revision"],
            "enabled": True,
            "model_id": bench["model_id"],
            "benchmark_id": bench["id"],
            **overrides,
        },
    )


@pytest.mark.parametrize("task", ["general", "coding", "analysis", "vision"])
def test_fixed_probes_measure_and_assignment_persists(probes, task):
    fixture, settings, activate = probes
    app, client, _, requests, _, default, _ = fixture
    row = enabled(fixture)
    activate()
    result = diagnostic(client, row, task)
    assert result.status_code == 200, result.text
    bench = result.json()
    assert bench["passed"] and bench["details"]["tools_passed"]
    assert bench["tokens"] == 60 and bench["elapsed_ms"] >= 0
    assert len(bench["details"]["prompt_sha256"]) == 64
    assert "content" not in result.text and not default.calls
    assert assign(client, bench).status_code == 200
    restarted = TaskRoutingStore(
        ModelConnectionStore(app.state.model_connection_store.path)
    )
    decision, bound = restarted.resolve(task, tools=True, images=task == "vision")
    assert decision["model"] == bench["model_id"]
    assert decision["connection_revision"] == row["revision"]
    assert restarted.audit()[0]["event"] == "enabled"


@pytest.mark.parametrize("wrong", ["wrong", "tools_wrong"])
def test_failed_new_probe_withdraws_existing_assignment(probes, wrong):
    fixture, settings, activate = probes
    app, client, _, _, _, _, _ = fixture
    row = enabled(fixture)
    activate()
    bench = diagnostic(client, row).json()
    assert assign(client, bench).status_code == 200
    store = TaskRoutingStore(app.state.model_connection_store)
    _, bound = store.resolve("coding")
    settings[wrong] = True
    newer = diagnostic(client, row).json()
    assert not newer["passed"] and newer["details"]["failure"]
    assert assign(client, bench).status_code == 400
    with pytest.raises(ValueError, match="superseded"):
        store.resolve("coding")
    with pytest.raises(EngineConnectionError, match="changed"):
        bound.generate([Message(role=Role.USER, content="hi")], model=bench["model_id"])


def test_changed_connection_during_benchmark_never_records_validation(probes):
    fixture, settings, activate = probes
    app, client, _, _, _, _, _ = fixture
    row = enabled(fixture)
    activate()
    settings["mutate"] = lambda: app.state.model_connection_store.enable(
        row["id"], row["revision"], False, "admin"
    )
    assert diagnostic(client, row).status_code == 409
    assert not TaskRoutingStore(app.state.model_connection_store).benchmarks()


def test_only_administrators_can_measure_or_assign(probes):
    fixture, _, activate = probes
    _, client, headers, requests, _, _, _ = fixture
    row = enabled(fixture)
    activate()
    requests.clear()
    client.headers.update(headers["user"])
    assert client.get("/v1/model-routing").status_code == 403
    assert diagnostic(client, row).status_code == 403
    assert client.put("/v1/model-routing/tasks/coding", json={}).status_code == 403
    assert not requests


@pytest.mark.parametrize("stream", [False, True])
def test_authenticated_task_request_uses_assignment_and_manual_override_stays(
    probes, stream
):
    fixture, _, activate = probes
    app, client, headers, requests, _, default, _ = fixture
    a = enabled(fixture)
    b = enabled(fixture, "second", "192.168.1.20")
    activate()
    bench = diagnostic(client, b).json()
    assert assign(client, bench).status_code == 200
    client.headers.update(headers["user"])
    requests.clear()
    result = chat(client, "default:latest", routing_task="coding", stream=stream)
    assert result.status_code == 200, result.text
    assert "192.168.1.20" in result.text and bench["model_id"] in result.text
    if stream:
        assert "routing_decision" in result.text
    else:
        assert result.json()["routing"]["task"] == "coding"
    result = chat(client, model_id(a["id"], "qwen3.5:9b"))
    assert result.status_code == 200 and "127.0.0.1" in result.text
    assert not default.calls


def test_disabled_and_stale_assignments_never_fall_back(probes):
    fixture, _, activate = probes
    app, client, _, requests, _, default, _ = fixture
    assert chat(client, "default:latest", routing_task="coding").status_code == 409
    row = enabled(fixture)
    activate()
    bench = diagnostic(client, row).json()
    assert assign(client, bench).status_code == 200
    assert assign(client, bench, revision=1).status_code == 409
    app.state.model_connection_store.enable(row["id"], row["revision"], False, "admin")
    requests.clear()
    assert chat(client, "default:latest", routing_task="coding").status_code == 409
    assert not requests and not default.calls


def test_vision_and_tools_require_matching_diagnostic_coverage(probes):
    fixture, _, activate = probes
    app, client, _, _, state, _, _ = fixture
    state["caps"] = ["completion"]
    row = enabled(fixture)
    activate()
    bench = diagnostic(client, row).json()
    assert bench["passed"] and not bench["details"]["tools_tested"]
    assert assign(client, bench).status_code == 200
    store = TaskRoutingStore(app.state.model_connection_store)
    with pytest.raises(ValueError, match="tool-call"):
        store.resolve("coding", tools=True)
    with pytest.raises(ValueError, match="Vision"):
        store.resolve("coding", images=True)
    assert diagnostic(client, row, "vision").status_code == 503


@pytest.mark.parametrize("value", [None, "bad", [], [None], [{"function": []}]])
def test_malformed_tool_results_cannot_pass(value):
    assert not _tool_ok({"tool_calls": value})


def test_correctness_probe_is_strict_and_never_executes_code():
    assert not _answer_ok({"content": '{"answer":true}'}, 1)
    assert not _answer_ok({"content": '```json\n{"answer":1}\n```'}, 1)
    assert not _answer_ok({"content": '{"answer":1}', "finish_reason": "length"}, 1)
    assert _answer_ok({"content": '{"answer":1}'}, 1)


def test_v3_migration_preserves_enabled_server_and_defaults_tasks_disabled(probes):
    fixture, _, _ = probes
    app, _, _, _, _, _, _ = fixture
    row = enabled(fixture)
    path = app.state.model_connection_store.path
    with sqlite3.connect(path) as db:
        for table in ("model_task_rules", "model_benchmarks", "model_routing_audit"):
            db.execute(f"DROP TABLE {table}")
        db.execute("PRAGMA user_version=3")
    reopened = ModelConnectionStore(path)
    assert reopened.get(row["id"]) == row
    assert all(not r["enabled"] for r in TaskRoutingStore(reopened).rules())


def test_rules_cannot_borrow_another_task_or_model_benchmark(probes):
    fixture, _, activate = probes
    _, client, _, _, _, _, _ = fixture
    a = enabled(fixture)
    b = enabled(fixture, "second", "192.168.1.20")
    activate()
    bench = diagnostic(client, a).json()
    assert (
        assign(client, bench, model_id=model_id(b["id"], "qwen3.5:9b")).status_code
        == 400
    )
    assert (
        client.put(
            "/v1/model-routing/tasks/analysis",
            json={
                "revision": 1,
                "enabled": True,
                "model_id": bench["model_id"],
                "benchmark_id": bench["id"],
            },
        ).status_code
        == 400
    )


def test_rule_disable_blocks_later_calls_in_bound_run(probes):
    fixture, _, activate = probes
    app, client, _, requests, _, _, _ = fixture
    row = enabled(fixture)
    activate()
    bench = diagnostic(client, row).json()
    assert assign(client, bench).status_code == 200
    _, bound = TaskRoutingStore(app.state.model_connection_store).resolve("coding")
    assert assign(client, bench, enabled=False).status_code == 200
    requests.clear()
    with pytest.raises(EngineConnectionError, match="changed"):
        bound.generate([Message(role=Role.USER, content="hi")], model=bench["model_id"])
    assert not requests


def test_authorization_revoked_during_probe_prevents_recording(probes):
    fixture, settings, activate = probes
    app, client, _, _, _, _, _ = fixture
    row = enabled(fixture)
    activate()
    settings["mutate"] = lambda: app.state.auth_store.revoke_all_sessions("admin")
    assert diagnostic(client, row).status_code == 401
    assert not TaskRoutingStore(app.state.model_connection_store).benchmarks()


def test_task_routing_keeps_conversation_ownership_gate(probes, tmp_path):
    fixture, _, activate = probes
    app, client, headers, requests, _, default, _ = fixture
    from openjarvis.sessions.session import SessionStore

    app.state.session_store = SessionStore(str(tmp_path / "sessions.db"))
    row = enabled(fixture)
    activate()
    bench = diagnostic(client, row).json()
    assert assign(client, bench).status_code == 200
    project = client.post("/v1/projects", json={"name": "Admin private"})
    assert project.status_code == 200, project.text
    session = client.post(
        "/v1/sessions",
        json={
            "project_id": project.json()["project_id"],
            "title": "Private",
        },
    )
    assert session.status_code == 200, session.text
    client.headers.update(headers["user"])
    requests.clear()
    result = chat(
        client,
        "default:latest",
        routing_task="coding",
        session_id=session.json()["session_id"],
    )
    assert result.status_code in (403, 404), result.text
    assert not any(r.url.path == "/api/chat" for r in requests)
    assert not default.calls


def test_benchmark_concurrency_limit_rejects_second_probe(probes, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    fixture, _, activate = probes
    _, client, _, _, _, _, _ = fixture
    row = enabled(fixture)
    activate()
    from openjarvis.engine.model_benchmarks import run_diagnostic

    started, release = Event(), Event()

    def waiting(*args):
        started.set()
        assert release.wait(10)
        return run_diagnostic(*args)

    monkeypatch.setattr(
        "openjarvis.server.model_routing_router.run_diagnostic", waiting
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(diagnostic, client, row)
        assert started.wait(5)
        try:
            assert diagnostic(client, row).status_code == 409
        finally:
            release.set()
        assert first.result().status_code == 200


@pytest.mark.parametrize("stream", [False, True])
def test_task_agent_preserves_selected_server_and_restores_shared_engine(
    probes, stream
):
    from tests.server.test_configured_model_selection import Agent

    fixture, _, activate = probes
    app, client, _, _, _, default, _ = fixture
    row = enabled(fixture)
    activate()
    bench = diagnostic(client, row).json()
    assert assign(client, bench).status_code == 200
    agent = Agent(default)
    if stream:
        agent._tools = [object()]
    app.state.agent = agent
    result = chat(client, "default:latest", routing_task="coding", stream=stream)
    assert result.status_code == 200 and "127.0.0.1" in result.text
    assert agent._engine is default and agent._model == "default:latest"
    assert not default.calls


def test_task_assignment_does_not_bypass_evidence_gate(probes):
    fixture, _, activate = probes
    _, client, _, requests, _, default, _ = fixture
    row = enabled(fixture)
    activate()
    bench = diagnostic(client, row).json()
    assert assign(client, bench).status_code == 200
    requests.clear()
    result = chat(
        client,
        "default:latest",
        routing_task="coding",
        messages=[
            {
                "role": "user",
                "content": "What is the current weather in Boston?",
            }
        ],
    )
    assert result.status_code == 200
    assert not any(r.url.path == "/api/chat" for r in requests)
    assert not default.calls


def test_explicit_fallback_only_for_transport_preflight_and_remains_bound(
    probes, monkeypatch
):
    fixture, _, activate = probes
    app, client, _, requests, _, default, _ = fixture
    primary = enabled(fixture)
    secondary = enabled(fixture, "second", "192.168.1.20")
    activate()
    primary_bench = diagnostic(client, primary).json()
    secondary_bench = diagnostic(client, secondary).json()
    assert (
        assign(
            client,
            primary_bench,
            fallback_model_id=secondary_bench["model_id"],
            fallback_benchmark_id=secondary_bench["id"],
        ).status_code
        == 200
    )
    from openjarvis.engine.configured_models import (
        ConfiguredModelEngine,
        ConfiguredModelError,
        ConfiguredModelUnavailable,
    )

    original = ConfiguredModelEngine.check
    mode = {"failure": "transport"}

    def check(engine, **kwargs):
        if engine.connection["id"] == primary["id"] and kwargs.get("live"):
            if mode["failure"] == "transport":
                raise ConfiguredModelUnavailable("unavailable")
            raise ConfiguredModelError("Missing required capability")
        return original(engine, **kwargs)

    monkeypatch.setattr(ConfiguredModelEngine, "check", check)
    store = TaskRoutingStore(app.state.model_connection_store)
    decision, bound = store.resolve("coding", tools=True, live=True)
    assert (
        decision["fallback_used"]
        and decision["primary_model"] == primary_bench["model_id"]
    )
    requests.clear()
    bound.generate([Message(role=Role.USER, content="hello")], model=decision["model"])
    assert all(r.url.host == "192.168.1.20" for r in requests)
    assert not default.calls
    mode["failure"] = "capability"
    with pytest.raises(ConfiguredModelError, match="capability"):
        store.resolve("coding", live=True)
    # Revoking either rule/benchmark invalidates an already bound fallback.
    rule = next(r for r in store.rules() if r["task"] == "coding")
    store.update("coding", rule["revision"], False, "", "", "admin")
    requests.clear()
    with pytest.raises(EngineConnectionError, match="changed"):
        bound.generate(
            [Message(role=Role.USER, content="hello")], model=decision["model"]
        )
    assert not requests


def test_runtime_selection_and_scheduled_query_keep_system_isolated(probes):
    fixture, _, activate = probes
    app, client, _, _, _, default, _ = fixture
    row = enabled(fixture)
    activate()
    bench = diagnostic(client, row).json()
    assert assign(client, bench).status_code == 200
    from openjarvis.core.config import JarvisConfig
    from openjarvis.core.events import EventBus
    from openjarvis.core.routing_context import routing_metadata
    from openjarvis.system.core import JarvisSystem
    from openjarvis.traces.store import TraceStore

    config = JarvisConfig()
    config.security.model_connections_db_path = str(
        app.state.model_connection_store.path
    )
    traces = TraceStore(str(app.state.model_connection_store.path) + ".traces")
    system = JarvisSystem(
        config, EventBus(), default, "test", "default:latest", trace_store=traces
    )
    result = system.ask("say hello", model="task/coding", context=False)
    assert result["routing"]["model"] == bench["model_id"]
    assert system.engine is default and system.model == "default:latest"
    assert not default.calls and routing_metadata() == {}
    traces.close()


def test_expanded_suite_records_each_case_and_rejects_old_suite(probes):
    fixture, _, activate = probes
    app, client, _, _, _, _, _ = fixture
    row = enabled(fixture)
    activate()
    bench = diagnostic(client, row).json()
    assert len(bench["details"]["cases"]) == 3
    assert bench["details"]["score"] == 1.0
    assert all(c["passed"] and c["elapsed_ms"] >= 0 for c in bench["details"]["cases"])
    assert assign(client, bench).status_code == 200
    with app.state.model_connection_store.connection() as db:
        db.execute("UPDATE model_benchmarks SET suite_version='diagnostic-v1'")
    with pytest.raises(ValueError, match="passing"):
        TaskRoutingStore(app.state.model_connection_store).resolve("coding")


def test_task_acceptance_verifies_http_ws_and_durable_routing(probes, tmp_path):
    from contextlib import contextmanager

    from scripts.check_session_continuity import run_checks
    from starlette.websockets import WebSocketDisconnect
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    from openjarvis.sessions.session import SessionStore
    from openjarvis.traces.store import TraceStore

    fixture, _, activate = probes
    app, client, _, _, _, _, _ = fixture
    app.state.session_store = SessionStore(str(tmp_path / "sessions.db"))
    app.state.trace_store = TraceStore(str(tmp_path / "trace.db"))
    project = app.state.session_store.create_project("admin", "acceptance")
    row = enabled(fixture)
    activate()
    bench = diagnostic(client, row).json()
    assert assign(client, bench).status_code == 200
    # Use the nonstreaming provider path: the synthetic transport returns JSON.
    from openjarvis.engine.configured_models import ConfiguredModelEngine

    original = ConfiguredModelEngine.stream
    ConfiguredModelEngine.stream = None

    @contextmanager
    def connect_ws():
        with client.websocket_connect("/v1/chat/stream") as ws:

            class Adapter:
                def send(self, value):
                    ws.send_text(value)

                def recv(self, timeout=None):
                    try:
                        return ws.receive_text()
                    except WebSocketDisconnect as exc:
                        raise ConnectionClosedError(
                            Close(exc.code, ""), None, None
                        ) from exc

            yield Adapter()

    report = {"checks": []}
    try:
        run_checks(
            client,
            connect_ws,
            project_id=project.project_id,
            model="default:latest",
            timeout=5,
            report=report,
            routing_task="coding",
        )
        assert report["routing_models"] == [bench["model_id"]] * 3
        assert report["login_revoked"]
    finally:
        ConfiguredModelEngine.stream = original
        app.state.trace_store.close()
        app.state.session_store.close()


def test_managed_tick_retries_keep_one_selection_and_persist_reason(probes, tmp_path):
    from unittest.mock import patch

    from openjarvis.agents._stubs import AgentResult
    from openjarvis.agents.errors import RetryableError
    from openjarvis.agents.executor import AgentExecutor
    from openjarvis.agents.manager import AgentManager
    from openjarvis.core.config import JarvisConfig
    from openjarvis.core.events import EventBus
    from openjarvis.engine.runtime_selection import resolve_selection
    from openjarvis.system.core import JarvisSystem
    from openjarvis.traces.store import TraceStore

    fixture, _, activate = probes
    app, client, _, _, _, default, _ = fixture
    row = enabled(fixture)
    activate()
    bench = diagnostic(client, row).json()
    assert assign(client, bench).status_code == 200
    config = JarvisConfig()
    config.security.model_connections_db_path = str(
        app.state.model_connection_store.path
    )
    system = JarvisSystem(config, EventBus(), default, "test", "default:latest")
    manager = AgentManager(str(tmp_path / "agents.db"))
    traces = TraceStore(str(tmp_path / "tick-traces.db"))
    executor = AgentExecutor(manager, system.bus, trace_store=traces)
    executor.set_system(system)
    agent = manager.create_agent(
        "routed", config={"model": "task/coding", "mcp_tools": False}
    )
    calls = []

    class TaskAgent:
        accepts_tools = False

        def __init__(self, engine, model, **kwargs):
            self.engine, self.model = engine, model

        def run(self, text, **kwargs):
            calls.append(self.model)
            result = self.engine.generate(
                [Message(role=Role.USER, content="hello")], model=self.model
            )
            if len(calls) == 1:
                raise RetryableError("retry")
            return AgentResult(content=result["content"])

    with (
        patch("openjarvis.agents.AgentRegistry.get", return_value=TaskAgent),
        patch(
            "openjarvis.engine.runtime_selection.resolve_selection",
            wraps=resolve_selection,
        ) as resolve,
        patch("openjarvis.agents.executor.time.sleep"),
    ):
        executor.execute_tick(agent["id"])
    assert resolve.call_count == 1
    assert calls == [bench["model_id"]] * 2 and not default.calls
    message = manager.list_messages(agent["id"])[0]
    trace = traces.get(message["correlation"]["trace_id"])
    assert trace.metadata["routing"]["task"] == "coding"
    assert trace.model == bench["model_id"]
    assert executor._toolkit_local.selection is None
    traces.close()
    manager.close()
