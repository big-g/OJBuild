"""Real-store role enforcement, management and personal-access regression tests."""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from openjarvis.core.events import EventBus
from openjarvis.server.account_admin_router import router
from openjarvis.server.app import create_app
from openjarvis.server.auth_middleware import AuthMiddleware
from openjarvis.server.auth_store import AuthStore
from openjarvis.server.role_policy import requires_admin
from openjarvis.server.ws_bridge import create_ws_router


@pytest.fixture(params=["", "master-secret"])
def setup(tmp_path, request):
    app = FastAPI()
    store = AuthStore(str(tmp_path / "auth.db"))
    app.state.auth_store = store
    app.state.api_key = request.param
    store.create_user("admin", "admin", "password123", is_admin=True)
    store.create_user("user", "user", "password123")
    app.add_middleware(AuthMiddleware, api_key=request.param)
    app.include_router(router)
    app.include_router(create_ws_router(EventBus()))
    tokens = {uid: store.create_session(uid) for uid in ["admin", "user"]}
    return app, store, tokens


def client(setup, uid):
    app, _, tokens = setup
    return TestClient(app, headers={"X-OpenJarvis-Session": tokens[uid]})


@pytest.mark.parametrize("uid", ["user", None, "key"])
@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/v1/auth/users", None),
        (
            "POST",
            "/v1/auth/users",
            {
                "username": "intruder",
                "display_name": "Intruder",
                "password": "password123",
                "role": "administrator",
            },
        ),
        ("PUT", "/v1/auth/users/user/role", {"role": "administrator"}),
        ("DELETE", "/v1/auth/users/admin", None),
    ],
)
def test_regular_missing_and_master_key_cannot_manage_accounts(
    setup, uid, method, path, body
):
    app, store, tokens = setup
    headers = {
        "Authorization": "Bearer master-secret",
        "X-User-Id": "admin",
        "X-Is-Admin": "true",
    }
    if uid == "user":
        headers["X-OpenJarvis-Session"] = tokens[uid]
    response = TestClient(app).request(method, path, headers=headers, json=body)
    assert response.status_code == (403 if uid == "user" else 401)
    assert not store.get_user("user")["is_admin"]
    assert store.get_user("admin") is not None
    assert store.get_user_by_username("intruder") is None


@pytest.mark.parametrize("role", ["user", "administrator"])
def test_admin_creates_explicit_roles_without_exposing_credentials(setup, role):
    c = client(setup, "admin")
    response = c.post(
        "/v1/auth/users",
        json={
            "username": "new",
            "display_name": "New",
            "password": "unique-secret123",
            "role": role,
        },
    )
    assert response.status_code == 201
    uid = response.json()["user_id"]
    assert bool(setup[1].get_user(uid)["is_admin"]) == (role == "administrator")
    listing = c.get("/v1/auth/users")
    assert listing.headers["cache-control"] == "no-store"
    assert "unique-secret" not in listing.text + response.text
    assert "password" not in listing.text + response.text
    assert "session" not in listing.text + response.text
    assert (
        c.post(
            "/v1/auth/users",
            json={
                "username": "new",
                "display_name": "Other",
                "password": "password123",
            },
        ).status_code
        == 409
    )
    assert setup[1].get_user(uid)["display_name"] == "New"


def test_default_user_and_safe_validation(setup):
    c = client(setup, "admin")
    body = {"username": "new", "display_name": "New", "password": "password123"}
    assert not c.post("/v1/auth/users", json=body).json()["is_admin"]
    response = c.post("/v1/auth/users", json={**body, "is_admin": True})
    assert response.status_code == 422
    assert "password123" not in response.text
    assert c.post("/v1/auth/users", json={**body, "role": "root"}).status_code == 422
    assert c.put("/v1/auth/users/user/role", json={}).status_code == 422


def test_role_changes_revoke_sessions_and_use_live_database_role(setup):
    app, store, tokens = setup
    extra = store.create_session("user")
    c = client(setup, "admin")
    assert (
        c.put("/v1/auth/users/user/role", json={"role": "administrator"}).status_code
        == 200
    )
    assert store.get_user_for_token(tokens["user"]) is None
    assert store.get_user_for_token(extra) is None
    token = store.create_session("user")
    promoted = TestClient(app, headers={"X-OpenJarvis-Session": token})
    assert promoted.get("/v1/auth/users").status_code == 200
    store.set_admin("user", False)
    assert promoted.get("/v1/auth/users").status_code == 403
    assert c.put("/v1/auth/users/user/role", json={"role": "user"}).status_code == 200
    assert promoted.get("/v1/auth/users").status_code == 401


def test_admin_cannot_remove_or_demote_self_and_delete_revokes_login(setup):
    c = client(setup, "admin")
    assert c.put("/v1/auth/users/admin/role", json={"role": "user"}).status_code == 400
    assert c.delete("/v1/auth/users/admin").status_code == 400
    assert c.delete("/v1/auth/users/user").status_code == 200
    assert setup[1].get_user_for_token(setup[2]["user"]) is None
    assert c.delete("/v1/auth/users/missing").status_code == 400


def test_store_rechecks_actor_inside_mutation_transaction(setup):
    store = setup[1]
    store.set_admin("admin", False)
    for action in [
        lambda: store.create_user(
            "bad", "bad", "password123", is_admin=True, actor_id="admin"
        ),
        lambda: store.change_role("admin", "user", True),
        lambda: store.delete_user("user", actor_id="admin"),
    ]:
        with pytest.raises(PermissionError):
            action()
    assert store.get_user("user") is not None
    assert store.get_user("bad") is None


@pytest.mark.parametrize(
    "path,method",
    [
        ("/v1/models/pull", "POST"),
        ("/v1/models/model", "DELETE"),
        ("/v1/budget/limits", "PUT"),
        ("/api/cloud/reload", "POST"),
        ("/v1/managed-agents", "POST"),
        ("/v1/approvals/id/approve", "POST"),
        ("/v1/skills", "POST"),
        ("/v1/future-setting", "PATCH"),
        ("/v1/sources-extra", "POST"),
        ("/v1/memory/search", "POST"),
        ("/v1/traces", "GET"),
    ],
)
def test_system_routes_reject_regular_user_before_handler(setup, path, method):
    app, _, _ = setup
    handler = MagicMock(return_value={"ok": True})

    def endpoint():
        return handler()

    app.add_api_route(path, endpoint, methods=[method])
    assert client(setup, "user").request(method, path).status_code == 403
    handler.assert_not_called()
    assert client(setup, "admin").request(method, path).status_code == 200
    handler.assert_called_once()


@pytest.mark.parametrize(
    "path",
    [
        "/v1/chat/completions",
        "/v1/projects",
        "/v1/sessions",
        "/v1/files",
        "/v1/sources",
        "/v1/auth/password",
        "/v1/research",
        "/v1/speech/transcribe",
    ],
)
def test_personal_actions_are_not_administrator_only(path):
    assert not requires_admin(path, "POST")
    assert not requires_admin(path + "/", "POST")


@pytest.mark.parametrize("uid", ["user", None, "key", "admin"])
def test_agent_event_socket_requires_human_admin(setup, uid):
    app, _, tokens = setup
    headers = {"Authorization": "Bearer master-secret"}
    if uid in tokens:
        headers["X-OpenJarvis-Session"] = tokens[uid]
    if uid == "admin":
        with TestClient(app).websocket_connect("/v1/agents/events", headers=headers):
            pass
    else:
        with pytest.raises(WebSocketDisconnect):
            with TestClient(app).websocket_connect(
                "/v1/agents/events", headers=headers
            ):
                pytest.fail("Unauthorized WebSocket accepted")


def test_actual_app_restricts_model_changes_and_keeps_personal_projects(tmp_path):
    engine = MagicMock()
    engine.engine_id = "mock"
    engine.list_models.return_value = ["test"]
    app = create_app(engine, "test")
    app.state.auth_store = AuthStore(str(tmp_path / "real.db"))
    store = app.state.auth_store
    store.create_user("user", "user", "password123")
    c = TestClient(app, headers={"X-OpenJarvis-Session": store.create_session("user")})
    assert c.post("/v1/models/pull", json={"model": "test"}).status_code == 403
    assert c.post("/v1/projects", json={"name": "Personal"}).status_code == 200
    assert c.get("/v1/files").status_code == 200


def test_feedback_cannot_change_another_users_trace(tmp_path, monkeypatch):
    from openjarvis.core import config
    from openjarvis.core.types import Trace
    from openjarvis.server import api_routes
    from openjarvis.traces.store import TraceStore

    monkeypatch.setattr(config, "DEFAULT_CONFIG_DIR", tmp_path)
    store = TraceStore(tmp_path / "traces.db")
    store.save(Trace(trace_id="owned", metadata={"correlation": {"user_id": "user"}}))
    store.save(
        Trace(trace_id="other", metadata={"correlation": {"user_id": "someone-else"}})
    )
    store.close()
    app = FastAPI()
    app.state.auth_store = AuthStore(tmp_path / "feedback-auth.db")
    app.state.auth_store.create_user("user", "user", "password123")
    app.include_router(api_routes.feedback_router)
    c = TestClient(
        app,
        headers={"X-OpenJarvis-Session": app.state.auth_store.create_session("user")},
    )
    assert (
        c.post("/v1/feedback", json={"trace_id": "owned", "score": 1}).status_code
        == 200
    )
    assert (
        c.post(
            "/v1/feedback",
            json={"trace_id": "other", "score": 1, "user_id": "someone-else"},
        ).status_code
        == 404
    )
    store = TraceStore(tmp_path / "traces.db")
    assert store.get("other").feedback is None
    store.close()


def test_browser_admin_session_and_midstream_revocation(setup):
    import base64

    from openjarvis.core.events import EventType

    app, store, tokens = setup
    bus = EventBus()
    isolated = FastAPI()
    isolated.state.auth_store = store
    isolated.state.api_key = "master-secret"
    isolated.include_router(create_ws_router(bus))
    encoded = base64.urlsafe_b64encode(tokens["admin"].encode()).decode().rstrip("=")
    protocols = ["openjarvis.session.v1", f"openjarvis.session.b64url.{encoded}"]
    with TestClient(isolated).websocket_connect(
        "/v1/agents/events", subprotocols=protocols
    ) as ws:
        assert ws.accepted_subprotocol == "openjarvis.session.v1"
        bus.publish(EventType.AGENT_TICK_START, {"agent_id": "admin-visible"})
        assert ws.receive_json()["data"]["agent_id"] == "admin-visible"
        store.set_admin("admin", False)
        bus.publish(EventType.AGENT_TICK_START, {"secret": "must not be delivered"})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_concurrent_administrators_cannot_demote_each_other_to_zero(setup):
    from concurrent.futures import ThreadPoolExecutor

    store = setup[1]
    store.set_admin("user", True)

    def demote(pair):
        try:
            store.change_role(pair[0], pair[1], False)
            return True
        except PermissionError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(demote, [("admin", "user"), ("user", "admin")]))
    assert sorted(outcomes) == [False, True]
    assert sum(bool(row["is_admin"]) for row in store.list_users()) == 1


def test_only_one_use_oauth_browser_gets_bypass_admin_header_gate():
    assert not requires_admin("/v1/connectors/gdrive/oauth/launch", "GET")
    assert not requires_admin("/v1/connectors/gdrive/oauth/callback", "GET")
    assert requires_admin("/v1/connectors/gdrive/oauth/start", "GET")
    assert requires_admin("/v1/connectors/gdrive/oauth/status", "GET")
    assert requires_admin("/v1/connectors/gdrive/oauth/callback", "POST")
    assert requires_admin("/v1/connectors/gdrive/oauth/callback/extra", "GET")
