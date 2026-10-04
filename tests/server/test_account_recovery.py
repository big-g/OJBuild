"""Recovery possession, password proof, and atomic credential invalidation."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.server.auth_middleware import AuthMiddleware
from openjarvis.server.auth_routes import router
from openjarvis.server.auth_store import AuthStore


@pytest.fixture
def account(tmp_path):
    store = AuthStore(tmp_path / "auth.db")
    store.create_user("owner", "gary", "old-password")
    token = store.create_session("owner")
    app = FastAPI()
    app.state.auth_store = store
    app.add_middleware(AuthMiddleware, api_key="master")
    app.include_router(router)
    return store, token, TestClient(app)


def test_web_issue_retrieve_reset_and_replay(account):
    store, token, client = account
    response = client.post(
        "/v1/auth/recovery-code",
        headers={"X-OpenJarvis-Session": token},
        json={"current_password": "old-password"},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    code = response.json()["recovery_code"]
    with store._connect() as conn:
        row = conn.execute("SELECT * FROM account_recovery").fetchone()
        assert row["code_hash"] != code
    found = client.post("/v1/auth/recover", json={"recovery_code": code})
    assert found.json() == {"username": "gary", "password_reset": False}
    response = client.post(
        "/v1/auth/recover", json={"recovery_code": code, "new_password": "new-password"}
    )
    assert response.status_code == 200
    assert store.authenticate("gary", "new-password")["user_id"] == "owner"
    assert store.authenticate("gary", "old-password") is None
    assert store.get_user_for_token(token) is None
    assert (
        client.post("/v1/auth/recover", json={"recovery_code": code}).status_code == 400
    )


@pytest.mark.parametrize("kind", ["missing", "master", "wrong-password"])
def test_password_proof_cannot_be_bypassed(account, kind):
    store, token, client = account
    headers = (
        {}
        if kind == "missing"
        else (
            {"Authorization": "Bearer master"}
            if kind == "master"
            else {"X-OpenJarvis-Session": token}
        )
    )
    for path in ["/v1/auth/recovery-code", "/v1/auth/password"]:
        response = client.post(
            path,
            headers=headers,
            json={"current_password": "wrong", "new_password": "new-password"},
        )
        assert response.status_code in {400, 401}
    assert store.authenticate("gary", "old-password") is not None
    assert store.get_user_for_token(token) is not None


def test_change_password_cancels_logins_and_recovery_codes(account):
    store, token, client = account
    code = store.issue_recovery_code("owner")["recovery_code"]
    response = client.post(
        "/v1/auth/password",
        headers={"X-OpenJarvis-Session": token},
        json={"current_password": "old-password", "new_password": "new-password"},
    )
    assert response.status_code == 200
    assert store.get_user_for_token(token) is None
    assert store.authenticate("gary", "new-password") is not None
    with pytest.raises(ValueError):
        store.recover_account(code)


@pytest.mark.parametrize("mode", ["expired", "disabled", "replaced", "admin-reset"])
def test_invalid_recovery_codes_cannot_retrieve_or_reset(account, mode):
    store, token, client = account
    code = store.issue_recovery_code("owner")["recovery_code"]
    if mode == "replaced":
        store.issue_recovery_code("owner")
    elif mode == "admin-reset":
        store.set_password("owner", "admin-password")
    else:
        with store._connect() as conn:
            conn.execute(
                "UPDATE account_recovery SET expires_at = 0"
                if mode == "expired"
                else "UPDATE users SET disabled = 1"
            )
    for data in [
        {"recovery_code": code},
        {"recovery_code": code, "new_password": "new-password"},
    ]:
        response = client.post("/v1/auth/recover", json=data)
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid or expired recovery code"


def test_code_consumption_is_atomic(account):
    store, _, _ = account
    code = store.issue_recovery_code("owner")["recovery_code"]

    def reset(_):
        try:
            store.recover_account(code, "new-password")
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(reset, range(2))) == [False, True]
