"""HTTP identities match persisted traces and verified cross-client conversations."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from openjarvis.agents.research_loop import ResearchResult
from openjarvis.core.correlation import (
    ExecutionIdentity,
    current_identity,
    execution_scope,
)
from openjarvis.server.app import create_app
from openjarvis.sessions.session import SessionStore
from openjarvis.traces.store import TraceStore
from tests.server.helpers import authenticated_client
from tests.server.test_routes import _make_agent, _make_engine, _traces_enabled_config


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("agent", [False, True])
def test_chat_headers_match_trace_and_verified_session_across_clients(
    tmp_path, stream, agent
):
    app = create_app(
        _make_engine(),
        "test-model",
        agent=_make_agent() if agent else None,
        config=_traces_enabled_config(tmp_path),
        api_key="master-test-key",
    )
    app.state.session_store = SessionStore(str(tmp_path / "sessions.db"))
    with authenticated_client(app) as client:
        project = app.state.session_store.create_project("test-user", "Project")
        session = app.state.session_store.create_session(
            "test-user", project.project_id
        )
        seen = []
        for device in ["browser", "android"]:
            response = client.post(
                "/v1/chat/completions",
                headers={
                    "X-Request-ID": "forged",
                    "X-Trace-ID": "forged",
                    "X-Turn-ID": "forged",
                    "X-User-ID": "other-user",
                    "X-Conversation-ID": "other-conversation",
                    "Origin": "http://localhost:3000",
                },
                json={
                    "model": "test-model",
                    "messages": [{"role": "user", "content": f"Hello {device}"}],
                    "session_id": session.session_id,
                    "stream": stream,
                },
            )
            assert response.status_code == 200, response.text
            trace_id = response.headers["X-Trace-ID"]
            assert trace_id != "forged"
            trace = app.state.trace_store.get(trace_id)
            assert trace is not None
            identity = trace.metadata["correlation"]
            assert identity["request_id"] == response.headers["X-Request-ID"]
            assert identity["turn_id"] == response.headers["X-Turn-ID"]
            assert identity["user_id"] == "test-user"
            assert (
                identity["session_id"]
                == identity["conversation_id"]
                == session.session_id
            )
            assert "X-Trace-ID" in response.headers["Access-Control-Expose-Headers"]
            seen.append(identity)
        assert seen[0]["conversation_id"] == seen[1]["conversation_id"]
        assert seen[0]["request_id"] != seen[1]["request_id"]
        assert seen[0]["turn_id"] != seen[1]["turn_id"]
        messages = app.state.session_store.get_session(session.session_id).messages
        assert messages[-1].metadata["correlation"] == seen[1]
        assert messages[-2].metadata["correlation"] == seen[1]
        forged = client.patch(
            f"/v1/sessions/{session.session_id}/messages/metadata",
            json={
                "content": messages[-1].content,
                "metadata": {"correlation": {"user_id": "forged"}},
            },
        )
        assert forged.status_code == 400
        assert (
            app.state.session_store.get_session(session.session_id)
            .messages[-1]
            .metadata["correlation"]
            == seen[1]
        )


def test_failed_auth_is_correlated_without_invoking_engine(tmp_path):
    engine = _make_engine()
    app = create_app(
        engine, "test-model", config=_traces_enabled_config(tmp_path), api_key="master"
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions", json={"model": "test-model", "messages": []}
        )
    assert response.status_code == 401
    assert response.headers["X-Request-ID"] and response.headers["X-Trace-ID"]
    engine.generate.assert_not_called()
    assert app.state.trace_store.list_traces() == []


def test_foreign_session_is_not_attached_to_trace_or_execution(tmp_path):
    engine = _make_engine()
    app = create_app(engine, "test-model", config=_traces_enabled_config(tmp_path))
    app.state.session_store = SessionStore(str(tmp_path / "sessions.db"))
    with authenticated_client(app) as client:
        project = app.state.session_store.create_project("other-user", "Other")
        session = app.state.session_store.create_session(
            "other-user", project.project_id
        )
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
                "session_id": session.session_id,
            },
        )
    assert response.status_code == 403
    engine.generate.assert_not_called()
    assert app.state.trace_store.list_traces() == []


def test_research_sse_and_verified_conversation_share_server_identity(tmp_path):
    from openjarvis.server import research_router

    app = create_app(
        _make_engine(), "test-model", config=_traces_enabled_config(tmp_path)
    )
    observed = []
    app.state.session_store = SessionStore(str(tmp_path / "sessions.db"))

    async def fake_stream(query, **kwargs):
        identity = current_identity()
        observed.append(identity.metadata())
        yield research_router._sse({"type": "synthesis", "text": "Research answer"})
        yield research_router._sse({"type": "done", "usage": {}})

    with (
        authenticated_client(app) as client,
        patch.object(research_router, "_stream_research", fake_stream),
    ):
        project = app.state.session_store.create_project("test-user", "Project")
        session = app.state.session_store.create_session(
            "test-user", project.project_id
        )
        response = client.post(
            "/api/research",
            json={"query": "Summarize notes", "session_id": session.session_id},
        )
        assert response.status_code == 200, response.text
        frames = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert frames and all(frame["correlation"] == observed[0] for frame in frames)
        assert observed[0]["trace_id"] == response.headers["X-Trace-ID"]
        assert observed[0]["user_id"] == "test-user"
        assert observed[0]["conversation_id"] == session.session_id
        assert (
            app.state.session_store.get_session(session.session_id)
            .messages[-1]
            .metadata["correlation"]
            == observed[0]
        )


@pytest.mark.parametrize("fail", [False, True])
def test_research_worker_persists_correlated_success_or_failure(
    tmp_path, monkeypatch, fail
):
    from openjarvis.server import research_router as module

    class Agent:
        def __init__(self, **kwargs):
            self.emit = kwargs["on_event"]

        def run(self, query):
            if fail:
                raise RuntimeError("protected-worker-secret")
            self.emit(
                {
                    "type": "final_answer",
                    "text": "Answer",
                    "sources": [],
                    "evidence": {},
                }
            )
            return ResearchResult(answer="Answer", iterations=1, tool_calls=[])

    monkeypatch.setattr(
        module, "_build_planner_engine", lambda *a, **kw: ("test", object(), "model")
    )
    monkeypatch.setattr(module, "KnowledgeStore", lambda: object())
    monkeypatch.setattr(module, "HybridSearch", lambda *a: object())
    monkeypatch.setattr(
        module, "OllamaEmbedder", lambda: SimpleNamespace(is_available=lambda: False)
    )
    monkeypatch.setattr(module, "ResearchAgent", Agent)
    totals = {"energy_j": 0, "mean_power_w": 0, "peak_power_w": 0}
    monkeypatch.setattr(
        module,
        "_LiveGPUSampler",
        lambda **kw: SimpleNamespace(
            start=lambda: None, stop=lambda: totals, available=False
        ),
    )
    monkeypatch.setattr(module, "_record_research_telemetry", lambda **kw: None)
    store = TraceStore(tmp_path / "traces.db")
    identity = ExecutionIdentity(user_id="verified")

    async def consume():
        with execution_scope(identity):
            return [
                frame
                async for frame in module._stream_research("Query", trace_store=store)
            ]

    frames = asyncio.run(consume())
    trace = store.get(identity.trace_id)
    assert trace and trace.metadata["correlation"] == identity.metadata()
    assert trace.agent == "research"
    assert trace.outcome == ("failure" if fail else None)
    assert "protected-worker-secret" not in str(trace)
    assert all(
        json.loads(frame[6:])["correlation"]["trace_id"] == trace.trace_id
        for frame in frames
    )


def test_parallel_agent_streams_exclude_other_request_events():
    import threading

    from openjarvis.agents._stubs import AgentResult
    from openjarvis.core.events import EventBus, EventType
    from openjarvis.server.models import ChatCompletionRequest
    from openjarvis.server.stream_bridge import AgentStreamBridge

    bus = EventBus()
    barrier = threading.Barrier(2)

    class Agent:
        _model = "test-model"

        def run(self, query, **kwargs):
            barrier.wait(timeout=5)
            bus.publish(
                EventType.TOOL_CALL_START,
                {"tool": "probe", "arguments": {"query": query}},
            )
            return AgentResult(content=query, turns=1)

    async def consume(query):
        request = ChatCompletionRequest(
            model="test-model", messages=[{"role": "user", "content": query}]
        )
        bridge = AgentStreamBridge(Agent(), bus, "test-model", request)
        return [frame async for frame in bridge.stream()]

    async def both():
        return await asyncio.gather(consume("first"), consume("second"))

    streams = asyncio.run(both())
    identities = []
    for query, frames in zip(["first", "second"], streams):
        events = [
            json.loads(frame.split("data: ", 1)[1])
            for frame in frames
            if frame.startswith("event: tool_call_start")
        ]
        assert len(events) == 1
        assert json.loads(events[0]["arguments"])["query"] == query
        identities.append(events[0]["correlation"]["trace_id"])
    assert len(set(identities)) == 2


def test_middleware_cancellation_restores_execution_context():
    from openjarvis.server.correlation import CorrelationMiddleware

    async def app(scope, receive, send):
        assert current_identity() is not None
        raise asyncio.CancelledError()

    async def run():
        before = current_identity()
        with pytest.raises(asyncio.CancelledError):
            await CorrelationMiddleware(app)({"type": "http"}, None, None)
        assert current_identity() is before

    asyncio.run(run())
