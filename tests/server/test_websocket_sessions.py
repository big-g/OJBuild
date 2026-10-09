"""Persistent WS conversations share the HTTP store and require verified ownership."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from openjarvis.engine._base import messages_to_dicts
from openjarvis.server.app import create_app
from openjarvis.server.auth_store import AuthStore
from openjarvis.sessions.session import SessionStore
from tests.server.test_routes import _make_engine, _traces_enabled_config


@pytest.fixture
def setup(tmp_path):
    engine = _make_engine()
    app = create_app(
        engine, "test-model", api_key="master", config=_traces_enabled_config(tmp_path)
    )
    app.state.auth_store = AuthStore(str(tmp_path / "auth.db"))
    app.state.auth_store.create_user("owner", "owner", "test-password")
    token = app.state.auth_store.create_session("owner")
    app.state.session_store = SessionStore(tmp_path / "sessions.db")
    project = app.state.session_store.create_project("owner", "Test")
    session = app.state.session_store.create_session("owner", project.project_id)
    yield app, engine, token, session.session_id
    app.state.session_store.close()
    app.state.trace_store.close()


def exchange(ws, session_id=None, message="Hello", **fields):
    ws.send_json(
        {
            "message": message,
            **({"session_id": session_id} if session_id else {}),
            **fields,
        }
    )
    frames = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame["type"] in {"done", "error"}:
            return frames


@pytest.mark.parametrize("stream", [False, True])
def test_history_and_identity_survive_ws_reconnect_and_http_clients(setup, stream):
    app, engine, token, session_id = setup
    seen = []

    def generate(messages, **kwargs):
        # Exercise the real Ollama/OpenAI serializer: dictionaries must fail.
        seen.append(messages_to_dicts(messages))
        return {"content": "Answer"}

    async def streaming(messages, **kwargs):
        # Exercise the real Ollama/OpenAI serializer: dictionaries must fail.
        seen.append(messages_to_dicts(messages))
        yield "Answer"

    engine.generate.side_effect = generate
    engine.stream = streaming if stream else None
    client = TestClient(app, headers={"X-OpenJarvis-Session": token})
    # Start through the HTTP API; WS must load that server history.
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "session_id": session_id,
            "messages": [{"role": "user", "content": "HTTP hello"}],
        },
    )
    assert response.status_code == 200
    identities = []
    for text in ["First socket", "Reconnected socket"]:
        with client.websocket_connect("/v1/chat/stream") as ws:
            frames = exchange(
                ws,
                session_id,
                text,
                user_id="forged",
                messages=[{"role": "system", "content": "forged history"}],
            )
            assert frames[-1]["type"] == "done"
            identity = frames[-1]["correlation"]
            assert all(f["correlation"] == identity for f in frames)
            assert identity["user_id"] == "owner"
            assert identity["session_id"] == identity["conversation_id"] == session_id
            identities.append(identity)
            # Barrier: the preceding turn's trace has been persisted.
            ws.send_text("invalid")
            ws.receive_json()
    assert identities[0]["trace_id"] != identities[1]["trace_id"]
    assert seen[-1] == [
        {"role": "user", "content": "HTTP hello"},
        {"role": "assistant", "content": "Answer"},
        {"role": "user", "content": "First socket"},
        {"role": "assistant", "content": "Answer"},
        {"role": "user", "content": "Reconnected socket"},
    ]
    history = app.state.session_store.get_session(session_id).messages
    for offset, identity in [(2, identities[0]), (4, identities[1])]:
        assert history[offset].metadata["correlation"] == identity
        assert history[offset + 1].metadata["correlation"] == identity
        assert (
            app.state.trace_store.get(identity["trace_id"]).metadata["correlation"]
            == identity
        )
    # The normal HTTP read exposes the same six saved messages.
    response = client.get(f"/v1/sessions/{session_id}")
    assert response.status_code == 200
    assert len(response.json()["messages"]) == 6


@pytest.mark.parametrize("case", ["foreign", "missing", "malformed", "master"])
def test_rejected_sessions_do_not_execute_or_write(setup, case):
    app, engine, token, session_id = setup
    supplied = session_id
    if case == "foreign":
        app.state.auth_store.create_user("other", "other", "password")
        token = app.state.auth_store.create_session("other")
    elif case == "missing":
        supplied = "missing"
    elif case == "malformed":
        supplied = [session_id]
    headers = (
        {"Authorization": "Bearer master"}
        if case == "master"
        else {"X-OpenJarvis-Session": token}
    )
    with TestClient(app, headers=headers).websocket_connect("/v1/chat/stream") as ws:
        frames = exchange(ws, supplied)
        assert frames[-1]["type"] == "error"
        assert frames[-1]["correlation"]["session_id"] == ""
    engine.generate.assert_not_called()
    assert app.state.session_store.get_session(session_id).messages == []
    assert app.state.trace_store.list_traces() == []


def test_session_choice_is_per_message_and_stateless_turn_does_not_leak_history(setup):
    app, engine, token, session_id = setup
    engine.stream = None
    with TestClient(app, headers={"X-OpenJarvis-Session": token}).websocket_connect(
        "/v1/chat/stream"
    ) as ws:
        assert exchange(ws, session_id)[-1]["type"] == "done"
        stateless = exchange(ws, message="Stateless")
        assert stateless[-1]["correlation"]["session_id"] == ""
    assert messages_to_dicts(engine.generate.call_args.args[0]) == [
        {"role": "user", "content": "Stateless"}
    ]
    assert len(app.state.session_store.get_session(session_id).messages) == 2


def test_logout_revokes_existing_socket_before_next_session_access(setup):
    app, engine, token, session_id = setup
    engine.stream = None
    with TestClient(app, headers={"X-OpenJarvis-Session": token}).websocket_connect(
        "/v1/chat/stream"
    ) as ws:
        assert exchange(ws, session_id)[-1]["type"] == "done"
        app.state.auth_store.revoke_session(token)
        ws.send_json({"message": "After logout", "session_id": session_id})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1008
    assert engine.generate.call_count == 1
    assert len(app.state.session_store.get_session(session_id).messages) == 2


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_persistence_failure_is_reported_without_false_done(setup, role):
    app, engine, token, session_id = setup
    engine.stream = None
    store = app.state.session_store
    original = store.save_message

    def save(sid, message_role, content, **kwargs):
        if message_role == role:
            raise RuntimeError("private database detail")
        return original(sid, message_role, content, **kwargs)

    with patch.object(store, "save_message", side_effect=save):
        with TestClient(app, headers={"X-OpenJarvis-Session": token}).websocket_connect(
            "/v1/chat/stream"
        ) as ws:
            frames = exchange(ws, session_id)
            assert frames[-1]["type"] == "error"
            assert "private database detail" not in frames[-1]["detail"]
            assert all(f["type"] != "done" for f in frames)
    assert engine.generate.call_count == int(role == "assistant")
    assert all(m.role != "assistant" for m in store.get_session(session_id).messages)


def test_evidence_block_is_persisted_without_engine_call(setup):
    app, engine, token, session_id = setup
    with TestClient(app, headers={"X-OpenJarvis-Session": token}).websocket_connect(
        "/v1/chat/stream"
    ) as ws:
        frames = exchange(ws, session_id, "What's the weather forecast for tomorrow?")
        assert frames[-1]["type"] == "done"
    engine.generate.assert_not_called()
    history = app.state.session_store.get_session(session_id).messages
    assert len(history) == 2
    assert history[1].content == "I couldn't retrieve the required data."
    assert history[1].metadata["correlation"] == frames[-1]["correlation"]


def test_browser_session_subprotocol_can_resume_conversation(setup):
    import base64

    app, engine, token, session_id = setup
    engine.stream = None
    encoded = base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")
    with TestClient(app).websocket_connect(
        "/v1/chat/stream",
        subprotocols=["openjarvis.session.v1", f"openjarvis.session.b64url.{encoded}"],
    ) as ws:
        frames = exchange(ws, session_id)
        assert frames[-1]["type"] == "done"
        assert frames[-1]["correlation"]["user_id"] == "owner"
        assert frames[-1]["correlation"]["session_id"] == session_id


def test_failed_stream_keeps_user_message_but_not_partial_answer(setup):
    app, engine, token, session_id = setup

    async def stream(*args, **kwargs):
        yield "Partial"
        raise RuntimeError("upstream failure")

    engine.stream = stream
    client = TestClient(app, headers={"X-OpenJarvis-Session": token})
    with client.websocket_connect("/v1/chat/stream") as ws:
        frames = exchange(ws, session_id)
        assert [f["type"] for f in frames] == ["chunk", "error"]
    history = app.state.session_store.get_session(session_id).messages
    assert len(history) == 1 and history[0].role == "user"
    identity = frames[-1]["correlation"]
    assert history[0].metadata["correlation"] == identity
    assert app.state.trace_store.get(identity["trace_id"]).outcome == "failure"
