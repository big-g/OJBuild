"""Cloud credentials are admin-only, encrypted, revisioned and live."""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.core import cloud_providers as providers
from openjarvis.engine.live_cloud import LiveCloudEngine
from openjarvis.server.auth_middleware import AuthMiddleware
from openjarvis.server.auth_store import AuthStore
from openjarvis.server.cloud_providers_router import availability_router, router


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
    for key in providers.KEY_NAMES | {"GOOGLE_API_KEY", "OPENAI_CODEX_API_KEY"}:
        monkeypatch.delenv(key, raising=False)
    app = FastAPI()
    store = AuthStore(tmp_path / "auth.db")
    app.state.auth_store = store
    app.state.api_key = "master-secret"
    store.create_user("admin", "admin", "password123", is_admin=True)
    store.create_user("user", "user", "password123")
    app.add_middleware(AuthMiddleware, api_key="master-secret")
    app.include_router(router)
    app.include_router(availability_router)
    tokens = {who: store.create_session(who) for who in ("admin", "user")}
    return app, tokens, tmp_path


def client(configured, who="admin"):
    return TestClient(
        configured[0], headers={"X-OpenJarvis-Session": configured[1][who]}
    )


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
@pytest.mark.parametrize("who", ["user", "master", "anonymous"])
def test_non_admin_cannot_read_or_change(configured, method, who):
    app, tokens, _ = configured
    headers = {"X-User-Id": "admin"}
    if who == "user":
        headers["X-OpenJarvis-Session"] = tokens["user"]
    if who == "master":
        headers["Authorization"] = "Bearer master-secret"
    response = TestClient(app).request(
        method,
        "/v1/cloud/providers" + ("" if method == "GET" else "/openai"),
        headers=headers,
        json={"revision": 0, "key": "secret"},
    )
    assert response.status_code in {401, 403}
    assert not (configured[2] / "cloud-provider-keys.json").exists()


def test_save_rotate_delete_and_persist_without_exposing_secret(
    configured, monkeypatch
):
    c = client(configured)
    secret = "sk-private-first-test-key"
    response = c.put("/v1/cloud/providers/openai", json={"revision": 0, "key": secret})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert secret not in response.text
    assert providers.resolve_keys()["OPENAI_API_KEY"] == secret
    root = configured[2]
    assert secret.encode() not in (root / "source_credentials.db").read_bytes()
    assert secret not in (root / "cloud-provider-keys.json").read_text()
    assert (root / "source_credentials.key").stat().st_mode & 0o777 == 0o600
    assert (
        c.put(
            "/v1/cloud/providers/openai", json={"revision": 0, "key": "stale-secret"}
        ).status_code
        == 409
    )
    response = c.put(
        "/v1/cloud/providers/openai", json={"revision": 1, "key": "new-secret"}
    )
    assert response.status_code == 200
    assert providers.resolve_keys()["OPENAI_API_KEY"] == "new-secret"
    monkeypatch.setenv("OPENAI_API_KEY", "old-environment-secret")
    response = c.request("DELETE", "/v1/cloud/providers/openai", json={"revision": 2})
    assert response.status_code == 200
    assert providers.resolve_keys()["OPENAI_API_KEY"] == ""
    # New vault instance, no process environment mutation required.
    assert (
        not client(configured)
        .get("/v1/cloud/providers")
        .json()["providers"][0]["configured"]
    )
    assert "new-secret" not in response.text
    assert len(response.json()["audit"]) == 3


@pytest.mark.parametrize(
    "key", ["", "has whitespace", "bad\nsecret", 123, {"secret": "sensitive"}]
)
def test_invalid_secrets_not_echoed(configured, key):
    response = client(configured).put(
        "/v1/cloud/providers/openai", json={"revision": 0, "key": key}
    )
    assert response.status_code in {400, 422}
    assert "sensitive" not in response.text
    assert "bad\\nsecret" not in response.text


def test_legacy_status_and_google_alias_removal(configured, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "old-google-key")
    (configured[2] / "cloud-keys.env").write_text("OPENAI_API_KEY=file-key\n")
    c = client(configured)
    rows = c.get("/v1/cloud/providers").json()["providers"]
    assert next(p for p in rows if p["id"] == "google")["source"] == "legacy"
    assert (
        c.request(
            "DELETE", "/v1/cloud/providers/google", json={"revision": 0}
        ).status_code
        == 200
    )
    assert not providers.resolve_keys().get("GEMINI_API_KEY")
    assert not providers.resolve_keys().get("GOOGLE_API_KEY")
    assert providers.resolve_keys()["OPENAI_API_KEY"] == "file-key"


def test_missing_vault_key_never_falls_back_to_environment(configured, monkeypatch):
    c = client(configured)
    c.put("/v1/cloud/providers/openai", json={"revision": 0, "key": "saved-secret"})
    (configured[2] / "source_credentials.key").unlink()
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-secret")
    with pytest.raises(ValueError):
        providers.resolve_keys()
    assert c.get("/v1/cloud/providers").status_code == 400


def test_live_engine_new_requests_use_rotated_keys(configured, monkeypatch):
    engines = []

    def engine(*, keys):
        instance = MagicMock()
        instance.generate.return_value = {"content": keys.get("OPENAI_API_KEY")}
        engines.append(instance)
        return instance

    monkeypatch.setattr("openjarvis.engine.cloud.CloudEngine", engine)
    live = LiveCloudEngine()
    providers.change_key("openai", "first-secret", 0, "admin")
    assert live.generate([], model="gpt-test")["content"] == "first-secret"
    providers.change_key("openai", "rotated-secret", 1, "admin")
    assert live.generate([], model="gpt-test")["content"] == "rotated-secret"
    providers.change_key("openai", None, 2, "admin")
    assert not live.generate([], model="gpt-test")["content"]
    assert all(instance.close.called for instance in engines)


def test_unknown_provider_does_not_write(configured):
    assert (
        client(configured)
        .put("/v1/cloud/providers/arbitrary", json={"revision": 0, "key": "secret"})
        .status_code
        == 400
    )
    assert providers.public_settings()["revision"] == 0


def test_ordinary_account_sees_availability_without_keys_or_audit(configured):
    providers.change_key("openai", "private-provider-key", 0, "admin")
    response = client(configured, "user").get("/v1/cloud/availability")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["providers"][0]["configured"] is True
    assert "private-provider-key" not in response.text
    assert "audit" not in response.text
    assert "actor" not in response.text


def test_vault_optimistic_revision_prevents_first_write_race(configured):
    from openjarvis.connectors.source_store import SourceConflict
    from openjarvis.connectors.token_vault import TokenVault

    path = configured[2] / "race-keys.json"
    first = TokenVault(path)
    second = TokenVault(path)
    first.save({"key": "first"}, expected_revision=0)
    with pytest.raises(SourceConflict):
        second.save({"key": "second"}, expected_revision=0)
    assert first.load()["key"] == "first"


@pytest.mark.asyncio
async def test_live_stream_snapshot_survives_rotation(configured, monkeypatch):
    from types import SimpleNamespace

    captured = []

    async def stream(messages, model, **kwargs):
        captured.append(model)
        yield "first chunk"
        yield "second chunk"

    def engine(*, keys):
        captured.append(keys["OPENAI_API_KEY"])
        return SimpleNamespace(stream=stream, close=lambda: captured.append("closed"))

    monkeypatch.setattr("openjarvis.engine.cloud.CloudEngine", engine)
    providers.change_key("openai", "old-key", 0, "admin")
    stream_iter = LiveCloudEngine().stream([], model="gpt-test")
    assert await anext(stream_iter) == "first chunk"
    providers.change_key("openai", "new-key", 1, "admin")
    assert await anext(stream_iter) == "second chunk"
    await stream_iter.aclose()
    assert captured == ["old-key", "gpt-test", "closed"]


@pytest.mark.parametrize("contents", ["not json", "{}"])
def test_corrupt_managed_reference_blocks_legacy_fallback(
    configured, monkeypatch, contents
):
    (configured[2] / "cloud-provider-keys.json").write_text(contents)
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-secret")
    with pytest.raises(ValueError):
        providers.resolve_keys()
