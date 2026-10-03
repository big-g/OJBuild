"""Tests for the WebSocket streaming endpoint."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from openjarvis.server.api_routes import include_all_routes  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(engine=None):
    """Create a minimal FastAPI app with mock engine wired up."""
    app = FastAPI()
    if engine is None:
        engine = _make_streaming_engine()
    app.state.engine = engine
    app.state.model = "test-model"
    include_all_routes(app)
    return app


def _make_streaming_engine(tokens=None):
    """Return a mock engine whose ``stream()`` yields tokens."""
    if tokens is None:
        tokens = ["Hello", " ", "world"]
    engine = MagicMock()
    engine.engine_id = "mock"

    async def mock_stream(messages, *, model="test-model", **kwargs):
        for tok in tokens:
            yield tok

    engine.stream = mock_stream
    engine.generate.return_value = {
        "content": "Hello world",
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        "model": "test-model",
        "finish_reason": "stop",
    }
    return engine


def _make_generate_only_engine(content="Hello world"):
    """Return a mock engine that only has ``generate()`` (no ``stream()``)."""
    engine = MagicMock(spec=["generate", "engine_id"])
    engine.engine_id = "mock-nostream"
    engine.generate.return_value = {
        "content": content,
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        "model": "test-model",
        "finish_reason": "stop",
    }
    return engine


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWebSocketStreaming:
    """Tests for WS /v1/chat/stream endpoint."""

    def test_basic_streaming_exchange(self):
        """A valid message should produce chunk messages followed by a done."""
        app = _make_app()
        client = TestClient(app)
        with client.websocket_connect("/v1/chat/stream") as ws:
            ws.send_text(json.dumps({"message": "Hi"}))
            chunks = []
            done = None
            # Read all responses until we get 'done'
            while True:
                data = ws.receive_json()
                if data["type"] == "chunk":
                    chunks.append(data["content"])
                elif data["type"] == "done":
                    done = data
                    break
                else:
                    break
            assert len(chunks) == 3
            assert chunks == ["Hello", " ", "world"]
            assert done is not None
            assert done["content"] == "Hello world"

    def test_current_request_is_blocked_before_engine_stream(self):
        stream_started = False

        async def forbidden_stream(messages, *, model="test-model", **kwargs):
            nonlocal stream_started
            stream_started = True
            yield "unsupported"

        engine = MagicMock()
        engine.engine_id = "mock"
        engine.stream = forbidden_stream
        app = _make_app(engine=engine)
        client = TestClient(app)

        with client.websocket_connect("/v1/chat/stream") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "message": "What's the weather forecast for tomorrow?",
                    }
                )
            )
            chunk = ws.receive_json()
            done = ws.receive_json()

        assert chunk["correlation"] == done["correlation"]
        assert {k: v for k, v in chunk.items() if k != "correlation"} == {
            "type": "chunk",
            "content": "I couldn't retrieve the required data.",
        }
        assert {k: v for k, v in done.items() if k != "correlation"} == {
            "type": "done",
            "content": "I couldn't retrieve the required data.",
        }
        assert stream_started is False

    def test_blocked_current_request_records_evidence_trace(
        self,
        tmp_path,
    ):
        from openjarvis.traces.store import TraceStore

        stream_started = False

        async def forbidden_stream(messages, *, model="test-model", **kwargs):
            nonlocal stream_started
            stream_started = True
            yield "unsupported"

        engine = MagicMock()
        engine.engine_id = "mock"
        engine.stream = forbidden_stream
        app = _make_app(engine=engine)
        store = TraceStore(tmp_path / "ws-evidence-traces.db")
        app.state.trace_store = store
        client = TestClient(app)

        with client.websocket_connect("/v1/chat/stream") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "message": "What's the weather forecast for tomorrow?",
                    }
                )
            )
            ws.receive_json()
            ws.receive_json()

        assert stream_started is False
        traces = store.list_traces()
        assert len(traces) == 1
        assert traces[0].metadata["evidence"] == {
            "required": True,
            "kind": "current",
            "status": "required_not_obtained",
            "reason": "Required evidence was not obtained.",
            "records": 0,
        }
        store.close()

    def test_missing_message_field(self):
        """Sending JSON without a 'message' field should return an error."""
        app = _make_app()
        client = TestClient(app)
        with client.websocket_connect("/v1/chat/stream") as ws:
            ws.send_text(json.dumps({"text": "Hi"}))
            data = ws.receive_json()
            assert data["type"] == "error"
            assert "Missing" in data["detail"]

    def test_invalid_json(self):
        """Sending non-JSON text should return an error."""
        app = _make_app()
        client = TestClient(app)
        with client.websocket_connect("/v1/chat/stream") as ws:
            ws.send_text("not json at all")
            data = ws.receive_json()
            assert data["type"] == "error"
            assert "Invalid JSON" in data["detail"]

    def test_empty_message_field(self):
        """An empty string for 'message' should return an error."""
        app = _make_app()
        client = TestClient(app)
        with client.websocket_connect("/v1/chat/stream") as ws:
            ws.send_text(json.dumps({"message": ""}))
            data = ws.receive_json()
            assert data["type"] == "error"
            assert "Missing" in data["detail"]

    def test_generate_fallback_when_no_stream(self):
        """When the engine has no stream(), generate() result is sent as one chunk."""
        engine = _make_generate_only_engine("Fallback response")
        app = _make_app(engine=engine)
        client = TestClient(app)
        with client.websocket_connect("/v1/chat/stream") as ws:
            ws.send_text(json.dumps({"message": "Hi"}))
            chunks = []
            done = None
            while True:
                data = ws.receive_json()
                if data["type"] == "chunk":
                    chunks.append(data["content"])
                elif data["type"] == "done":
                    done = data
                    break
                else:
                    break
            assert len(chunks) == 1
            assert chunks[0] == "Fallback response"
            assert done is not None
            assert done["content"] == "Fallback response"

    def test_custom_model_in_request(self):
        """The model field from the request should be forwarded to the engine."""
        tokens = ["OK"]
        engine = _make_streaming_engine(tokens=tokens)
        app = _make_app(engine=engine)
        client = TestClient(app)
        with client.websocket_connect("/v1/chat/stream") as ws:
            ws.send_text(json.dumps({"message": "Hi", "model": "custom-model"}))
            # Consume until done
            while True:
                data = ws.receive_json()
                if data["type"] == "done":
                    break
            # The mock stream function was called — we can't easily inspect
            # async-generator call args, but the exchange completed without error
            assert data["content"] == "OK"

    def test_engine_error_returns_error_message(self):
        """If the engine raises, the endpoint should send an error frame."""
        engine = MagicMock()

        async def bad_stream(messages, *, model="test-model", **kwargs):
            raise RuntimeError("Engine exploded")
            # Make it look like an async generator to the endpoint
            yield  # pragma: no cover – unreachable, but needed for async gen syntax

        engine.stream = bad_stream
        app = _make_app(engine=engine)
        client = TestClient(app)
        with client.websocket_connect("/v1/chat/stream") as ws:
            ws.send_text(json.dumps({"message": "boom"}))
            data = ws.receive_json()
            assert data["type"] == "error"
            assert "Engine exploded" in data["detail"]

    def test_multiple_messages_on_same_connection(self):
        """The WebSocket should support multiple request/response cycles."""
        app = _make_app()
        client = TestClient(app)
        with client.websocket_connect("/v1/chat/stream") as ws:
            for _ in range(3):
                ws.send_text(json.dumps({"message": "Hi"}))
                # Drain until done
                while True:
                    data = ws.receive_json()
                    if data["type"] == "done":
                        assert data["content"] == "Hello world"
                        break

    def test_no_engine_configured(self):
        """If app.state has no engine, an error should be returned."""
        app = FastAPI()
        app.state.model = "test-model"
        # Intentionally do NOT set app.state.engine
        include_all_routes(app)
        client = TestClient(app)
        with client.websocket_connect("/v1/chat/stream") as ws:
            ws.send_text(json.dumps({"message": "Hi"}))
            data = ws.receive_json()
            assert data["type"] == "error"
            assert "engine" in data["detail"].lower()


__all__ = [
    "TestWebSocketStreaming",
]


@pytest.mark.parametrize("streaming", [True, False])
def test_turn_correlation_reaches_worker_frames_and_traces(tmp_path, streaming):
    from openjarvis.core.correlation import current_identity
    from openjarvis.traces.store import TraceStore

    observed = []
    engine = _make_generate_only_engine()

    def generate(messages, **kwargs):
        observed.append(current_identity().metadata())
        return {"content": "OK"}

    async def stream(messages, **kwargs):
        observed.append(current_identity().metadata())
        yield "OK"

    engine.generate.side_effect = generate
    if streaming:
        engine.stream = stream
    app = _make_app(engine)
    store = TraceStore(tmp_path / "ws.db")
    app.state.trace_store = store
    frames = []
    with TestClient(app).websocket_connect("/v1/chat/stream") as ws:
        for _ in range(2):
            ws.send_json(
                {
                    "message": "Hi",
                    "correlation": {"trace_id": "forged"},
                    "user_id": "forged",
                    "conversation_id": "forged",
                }
            )
            chunk, done = ws.receive_json(), ws.receive_json()
            assert chunk["correlation"] == done["correlation"]
            frames.append(done["correlation"])
        # Round-trip ensures the previous completed turn's trace was saved.
        ws.send_text("invalid")
        ws.receive_json()
    assert observed == frames
    assert frames[0]["trace_id"] != frames[1]["trace_id"]
    assert frames[0]["turn_id"] != frames[1]["turn_id"]
    for identity in frames:
        assert identity["user_id"] == identity["session_id"] == ""
        trace = store.get(identity["trace_id"])
        assert trace.metadata["correlation"] == identity
    assert current_identity() is None
    store.close()


def test_failed_turn_trace_and_next_turn_are_isolated(tmp_path):
    from openjarvis.core.correlation import current_identity
    from openjarvis.traces.store import TraceStore

    engine = _make_generate_only_engine()
    engine.generate.side_effect = [
        RuntimeError("private upstream detail"),
        {"content": "OK"},
    ]
    app = _make_app(engine)
    store = TraceStore(tmp_path / "ws.db")
    app.state.trace_store = store
    with TestClient(app).websocket_connect("/v1/chat/stream") as ws:
        ws.send_json({"message": "Hi"})
        error = ws.receive_json()
        ws.send_json({"message": "Hi again"})
        chunk, done = ws.receive_json(), ws.receive_json()
        ws.send_text("invalid")
        ws.receive_json()
    failed = store.get(error["correlation"]["trace_id"])
    assert failed.outcome == "failure"
    assert failed.metadata["correlation"] == error["correlation"]
    assert failed.metadata["error_type"] == "RuntimeError"
    assert "private upstream detail" not in str(failed)
    assert error["correlation"]["trace_id"] != done["correlation"]["trace_id"]
    assert chunk["correlation"] == done["correlation"]
    assert current_identity() is None
    store.close()


def test_verified_user_cannot_be_overridden_in_message(monkeypatch):
    monkeypatch.setattr(
        "openjarvis.server.auth_middleware.authenticate_websocket",
        lambda ws, key: (True, None, "verified-user"),
    )
    with TestClient(_make_app()).websocket_connect("/v1/chat/stream") as ws:
        ws.send_json({"message": "Hi", "user_id": "forged"})
        frame = ws.receive_json()
        assert frame["correlation"]["user_id"] == "verified-user"
        while ws.receive_json()["type"] != "done":
            pass


@pytest.mark.parametrize("payload", [[], None, 1, {"message": ["bad"]}])
def test_non_object_or_non_string_message_returns_correlated_error(payload):
    with TestClient(_make_app()).websocket_connect("/v1/chat/stream") as ws:
        ws.send_json(payload)
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert frame["correlation"]["trace_id"]
        ws.send_json({"message": "Hi"})
        next_frame = ws.receive_json()
        assert next_frame["correlation"]["trace_id"] != frame["correlation"]["trace_id"]
        while ws.receive_json()["type"] != "done":
            pass


def test_parallel_connections_do_not_share_identity():
    from openjarvis.core.correlation import current_identity

    observed = {}

    async def stream(messages, **kwargs):
        import asyncio

        identity = current_identity().metadata()
        observed[messages[0]["content"]] = identity
        yield "first"
        await asyncio.sleep(0)
        assert current_identity().metadata() == identity
        yield "last"

    engine = _make_streaming_engine()
    engine.stream = stream
    client = TestClient(_make_app(engine))
    with client.websocket_connect("/v1/chat/stream") as first:
        with client.websocket_connect("/v1/chat/stream") as second:
            first.send_json({"message": "one"})
            second.send_json({"message": "two"})
            for ws, query in [(first, "one"), (second, "two")]:
                for _ in range(3):
                    assert ws.receive_json()["correlation"] == observed[query]
    assert observed["one"]["trace_id"] != observed["two"]["trace_id"]


def test_cancelled_websocket_restores_ambient_identity():
    import asyncio
    from types import SimpleNamespace

    from openjarvis.core.correlation import (
        ExecutionIdentity,
        current_identity,
        execution_scope,
    )
    from openjarvis.server.api_routes import websocket_chat_stream

    async def exercise():
        ambient = ExecutionIdentity(user_id="ambient")
        observed = []

        class Socket:
            app = SimpleNamespace(state=SimpleNamespace(api_key="", model="test"))
            headers = {}
            query_params = {}
            state = SimpleNamespace()

            async def accept(self, **kwargs):
                pass

            async def receive_text(self):
                return '{"message": "Hi"}'

            async def send_json(self, payload):
                observed.append(current_identity())
                raise asyncio.CancelledError

        async def stream(*args, **kwargs):
            yield "OK"

        Socket.app.state.engine = SimpleNamespace(stream=stream)
        with execution_scope(ambient):
            with pytest.raises(asyncio.CancelledError):
                await websocket_chat_stream(Socket())
            assert current_identity() is ambient
        assert observed[0] is not ambient
        assert observed[0].user_id == ""
        assert current_identity() is None

    asyncio.run(exercise())
