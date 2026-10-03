"""Run the live acceptance workflow against real routes without outbound sockets."""

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from scripts.check_session_continuity import CheckFailed, run_checks
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from tests.server.test_websocket_sessions import setup  # noqa: F401


@pytest.mark.parametrize("tracing", [True, False])
def test_acceptance_workflow_and_missing_trace_fail_closed(setup, tracing):  # noqa: F811
    app, engine, token, session_id = setup
    engine.stream = None
    project_id = app.state.session_store.get_session(session_id).project_id
    saved_trace_store = app.state.trace_store
    if not tracing:
        app.state.trace_store = None
    client = TestClient(app, headers={"X-OpenJarvis-Session": token})

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
        if tracing:
            run_checks(
                client,
                connect_ws,
                project_id=project_id,
                model="test-model",
                timeout=5,
                report=report,
            )
            assert len(report["checks"]) == 4
            assert report["login_revoked"] is True
            assert app.state.auth_store.get_user_for_token(token) is None
            assert len(report["trace_ids"]) == 3
        else:
            with pytest.raises(CheckFailed, match="HTTP 404"):
                run_checks(
                    client,
                    connect_ws,
                    project_id=project_id,
                    model="test-model",
                    timeout=5,
                    report=report,
                )
        assert report["session_id"] != session_id
        assert (
            len(app.state.session_store.get_session(report["session_id"]).messages) == 6
        )
    finally:
        app.state.trace_store = saved_trace_store
