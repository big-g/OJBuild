"""Authenticated conversation continuity across clients and server restarts."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from openjarvis.agents._stubs import AgentResult
from openjarvis.core.config import JarvisConfig
from openjarvis.engine._stubs import StreamChunk
from openjarvis.server.app import create_app
from openjarvis.sessions.session import SessionStore
from tests.server.helpers import authenticated_client


def _app(db_path, *, with_agent=False):
    seen = []
    engine = MagicMock()
    engine.engine_id = "ollama"
    engine.health.return_value = True
    engine.list_models.return_value = ["test-model"]

    def generate(messages, **kwargs):
        seen.append([(m.role.value, m.content) for m in messages])
        return {"content": f"reply-{len(seen)}", "usage": {}}

    async def stream(messages, **kwargs):
        yield generate(messages)["content"]

    async def stream_full(messages, **kwargs):
        yield StreamChunk(content=generate(messages)["content"], finish_reason="stop")

    engine.generate.side_effect = generate
    engine.stream = stream
    engine.stream_full = stream_full
    agent = None
    if with_agent:
        agent = MagicMock()
        agent._model = "test-model"
        agent._tools = [MagicMock()]

        def run(text, context=None):
            messages = list(context.conversation.messages)
            seen.append(
                [(m.role.value, m.content) for m in messages] + [("user", text)]
            )
            return AgentResult(content=f"reply-{len(seen)}", turns=1)

        agent.run.side_effect = run
    config = JarvisConfig()
    config.analytics.enabled = False
    config.traces.enabled = False
    config.agent.context_from_memory = False
    app = create_app(engine, "test-model", agent=agent, config=config)
    app.state.session_store = SessionStore(db_path)
    return app, seen


def _session(client):
    project = client.post("/v1/projects", json={"name": "Continuity"})
    assert project.status_code == 200, project.text
    session = client.post(
        "/v1/sessions",
        json={
            "project_id": project.json()["project_id"],
            "title": "Shared conversation",
        },
    )
    assert session.status_code == 200, session.text
    return session.json()["session_id"]


@pytest.mark.parametrize("with_agent", [False, True])
@pytest.mark.parametrize(
    "first_stream,second_stream",
    [
        (False, False),
        (False, True),
        (True, False),
        (True, True),
    ],
)
def test_two_clients_resume_authoritative_history_after_restart(
    tmp_path, first_stream, second_stream, with_agent
):
    db_path = tmp_path / "sessions.db"
    app, seen = _app(db_path, with_agent=with_agent)
    try:
        first = authenticated_client(app)
        second = authenticated_client(app)
        assert (
            first.headers["X-OpenJarvis-Session"]
            != second.headers["X-OpenJarvis-Session"]
        )
        session_id = _session(first)
        first_response = first.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "session_id": session_id,
                "stream": first_stream,
                "messages": [{"role": "user", "content": "Remember my blue bicycle."}],
            },
        )
        assert first_response.status_code == 200, first_response.text
        loaded = second.get(f"/v1/sessions/{session_id}")
        assert loaded.status_code == 200
        assert [(m["role"], m["content"]) for m in loaded.json()["messages"]] == [
            ("user", "Remember my blue bicycle."),
            ("assistant", "reply-1"),
        ]
        metadata = {
            "local_message_id": "first-device-message",
            "telemetry": {"model_id": "test-model"},
        }
        updated = first.patch(
            f"/v1/sessions/{session_id}/messages/metadata",
            json={
                "content": "reply-1",
                "metadata": metadata,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["updated"] is True
        response = second.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "session_id": session_id,
                "stream": second_stream,
                # Stale client-side history must never replace server history.
                "messages": [
                    {"role": "user", "content": "Invented old message"},
                    {"role": "assistant", "content": "Invented old answer"},
                    {"role": "user", "content": "What did I ask you to remember?"},
                ],
            },
        )
        assert response.status_code == 200, response.text
        expected = [
            ("user", "Remember my blue bicycle."),
            ("assistant", "reply-1"),
            ("user", "What did I ask you to remember?"),
        ]
        assert [
            (role, content) for role, content in seen[-1] if role != "system"
        ] == expected
    finally:
        app.state.session_store.close()

    restarted, _ = _app(db_path, with_agent=with_agent)
    try:
        resumed = authenticated_client(restarted)
        detail = resumed.get(f"/v1/sessions/{session_id}")
        assert detail.status_code == 200
        assert [(m["role"], m["content"]) for m in detail.json()["messages"]] == [
            *expected,
            ("assistant", "reply-2"),
        ]
        assert detail.json()["messages"][1]["metadata"] == metadata
        listed = resumed.get("/v1/sessions")
        assert listed.status_code == 200
        assert any(s["session_id"] == session_id for s in listed.json()["sessions"])
    finally:
        restarted.state.session_store.close()


def test_raw_tool_stream_preserves_completed_text_for_other_client(tmp_path):
    app, _ = _app(tmp_path / "sessions.db")
    try:
        first, second = authenticated_client(app), authenticated_client(app)
        session_id = _session(first)
        response = first.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "session_id": session_id,
                "stream": True,
                "tools": [{"type": "function", "function": {"name": "lookup"}}],
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert response.status_code == 200
        assert "[DONE]" in response.text
        detail = second.get(f"/v1/sessions/{session_id}")
        assert detail.status_code == 200
        assert [(m["role"], m["content"]) for m in detail.json()["messages"]] == [
            ("user", "Hello"),
            ("assistant", "reply-1"),
        ]
    finally:
        app.state.session_store.close()


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("get", "/v1/projects", None),
        ("post", "/v1/projects", {"name": "Private"}),
        ("post", "/v1/sessions", {"project_id": "missing"}),
    ],
)
def test_session_setup_preserves_authentication_error(tmp_path, method, path, payload):
    app, _ = _app(tmp_path / "sessions.db")
    try:
        response = TestClient(app).request(method, path, json=payload)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Session"
    finally:
        app.state.session_store.close()
