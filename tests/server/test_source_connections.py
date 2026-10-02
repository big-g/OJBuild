"""Hermetic named account authorization; no real registrations or provider calls."""

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.connectors import oauth
from openjarvis.connectors.instance_sources import (
    ACCOUNT_READERS,
    AccountSource,
    token_path,
)
from openjarvis.connectors.source_manager import SourceManager
from openjarvis.connectors.source_store import SourceStore
from openjarvis.server.auth_middleware import AuthMiddleware
from openjarvis.server.sources_router import create_sources_router


@pytest.fixture
def setup(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Provider traffic forbidden")

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)
    monkeypatch.setattr(oauth, "get_client_credentials", lambda provider: None)
    manager = SourceManager(
        SourceStore(
            str(tmp_path / "sources.db"), legacy_path=str(tmp_path / "legacy.json")
        ),
        knowledge_path=str(tmp_path / "knowledge.db"),
    )
    app = FastAPI()
    app.add_middleware(AuthMiddleware, api_key="test-key")
    app.include_router(create_sources_router(manager))
    with TestClient(
        app, base_url="https://testserver", headers={"Authorization": "Bearer test-key"}
    ) as client:
        yield client, manager, tmp_path


def source(setup, service="gdrive", name="Work"):
    client, manager, directory = setup
    config = {"location": "Boston"} if service == "weather" else {}
    response = client.post(
        "/v1/sources",
        json={"adapter_id": service + "_account", "name": name, "config": config},
    )
    assert response.status_code == 201, response.text
    return response.json()


def path(record):
    return "/v1/sources/" + record["id"]


def client_registration(client, record):
    response = client.put(
        path(record) + "/connection/client",
        json={
            "revision": record["revision"],
            "client_id": "test-application",
            "client_secret": "protected-application-secret",
        },
    )
    assert response.status_code == 200, response.text
    record["revision"] += 1


def launch(client, record):
    begin = client.post(path(record) + "/oauth/start")
    assert begin.status_code == 200, begin.text
    handoff = client.get(
        begin.json()["launch_path"],
        headers={"Authorization": ""},
        follow_redirects=False,
    )
    assert handoff.status_code == 307, handoff.text
    params = parse_qs(urlparse(handoff.headers["location"]).query)
    return params, begin.json()


@pytest.mark.parametrize("service", ACCOUNT_READERS)
def test_every_named_reader_has_private_path_and_no_legacy_fallback(
    setup, service, monkeypatch
):
    client, manager, directory = setup
    record = source(setup, service)
    legacy = directory / "connectors" / "google.json"
    oauth.save_tokens(str(legacy), {"access_token": "legacy-account"})
    monkeypatch.setattr(oauth, "_SHARED_GOOGLE_CREDENTIALS_PATH", str(legacy))
    reader = AccountSource(service, record, directory)
    assert not reader.is_connected()
    actual = getattr(
        reader.reader, "_credentials_path", getattr(reader.reader, "_token_path", None)
    )
    assert str(actual) == str(token_path(directory, record["id"]))
    assert client.get(path(record) + "/connection").json()["connected"] is False


@pytest.mark.parametrize(
    "service", [s for s, item in ACCOUNT_READERS.items() if item[3] == "token"]
)
def test_token_connections_are_independent_encrypted_and_secret_free(setup, service):
    client, manager, directory = setup
    a, b = source(setup, service), source(setup, service, "Home")
    secret = "xoxp-protected-token" if service == "slack" else "protected-token-value"
    response = client.put(
        path(a) + "/connection/token", json={"revision": 1, "token": secret}
    )
    assert response.status_code == 200, response.text
    assert client.get(path(a) + "/connection").json()["connected"]
    assert not client.get(path(b) + "/connection").json()["connected"]
    assert secret not in token_path(directory, a["id"]).read_text()
    assert secret.encode() not in (directory / "source_credentials.db").read_bytes()
    assert secret not in client.get("/v1/sources").text
    assert secret not in client.get("/v1/sources/audit").text
    assert secret not in client.get(path(a) + "/connection").text
    assert (
        client.post(
            path(a) + "/connection/disconnect", json={"revision": 2}
        ).status_code
        == 200
    )
    assert not client.get(path(a) + "/connection").json()["connected"]


def test_instance_oauth_binds_account_callback_scopes_and_private_bundle(
    setup, monkeypatch
):
    client, manager, directory = setup
    a, b = source(setup), source(setup, name="Personal")
    client_registration(client, a)
    client_registration(client, b)
    params, attempt = launch(client, a)
    assert params["redirect_uri"] == [
        "https://testserver" + path(a) + "/oauth/callback"
    ]
    assert params["scope"] == ["https://www.googleapis.com/auth/drive.readonly"]
    assert params["code_challenge_method"] == ["S256"]
    calls = []

    def exchange(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "access_token": "private-account-access",
            "refresh_token": "private-refresh",
            "scope": params["scope"][0],
            "expires_in": 3600,
        }

    monkeypatch.setattr(oauth, "_exchange_token", exchange)
    query = {"state": params["state"][0], "code": "fake-code"}
    wrong = client.get(
        path(b) + "/oauth/callback", params=query, headers={"Authorization": ""}
    )
    assert wrong.status_code == 400
    callback = client.get(
        path(a) + "/oauth/callback", params=query, headers={"Authorization": ""}
    )
    assert callback.status_code == 200, callback.text
    assert len(calls) == 1 and calls[0][1]["code_verifier"]
    assert client.get(
        path(a) + "/oauth/status", params={"attempt_id": attempt["attempt_id"]}
    ).json() == {"status": "completed"}
    assert client.get(path(a) + "/connection").json()["connected"]
    assert not client.get(path(b) + "/connection").json()["connected"]
    assert "private-account-access" not in token_path(directory, a["id"]).read_text()
    assert not (directory / "connectors" / "gdrive.json").exists()
    assert client.get(path(a) + "/oauth/callback", params=query).status_code == 400


@pytest.mark.parametrize("mutation", ["disconnect", "delete", "edit", "registration"])
def test_stale_callback_cannot_restore_account_after_lifecycle_change(
    setup, monkeypatch, mutation
):
    client, manager, directory = setup
    record = source(setup)
    client_registration(client, record)
    params, attempt = launch(client, record)
    if mutation == "disconnect":
        response = client.post(
            path(record) + "/connection/disconnect",
            json={"revision": record["revision"]},
        )
    elif mutation == "delete":
        response = client.delete(path(record), params={"revision": record["revision"]})
    elif mutation == "registration":
        client_registration(client, record)
        response = None
    else:
        response = client.put(
            path(record),
            json={
                "revision": record["revision"],
                "name": "Changed",
                "config": {},
                "enabled": True,
            },
        )
    if response is not None:
        assert response.status_code in (200, 204), response.text

    def exchange(*args, **kwargs):
        raise AssertionError("Stale callbacks must not exchange")

    monkeypatch.setattr(oauth, "_exchange_token", exchange)
    callback = client.get(
        path(record) + "/oauth/callback",
        params={"state": params["state"][0], "code": "fake"},
        headers={"Authorization": ""},
    )
    assert callback.status_code in (400, 404, 409)
    assert not (oauth.load_tokens(str(token_path(directory, record["id"]))) or {}).get(
        "access_token"
    )


def test_account_endpoints_auth_validation_and_stale_revision(setup):
    client, manager, directory = setup
    record = source(setup, "oura")
    base = path(record)
    for method, suffix, body in [
        ("GET", "/connection", None),
        ("PUT", "/connection/token", {"revision": 1, "token": "secret"}),
        ("POST", "/connection/disconnect", {"revision": 1}),
        ("POST", "/oauth/start", None),
        ("GET", "/oauth/status", None),
    ]:
        assert (
            client.request(
                method, base + suffix, json=body, headers={"Authorization": ""}
            ).status_code
            == 401
        )
    assert (
        client.put(
            base + "/connection/token", json={"revision": 5, "token": "secret"}
        ).status_code
        == 409
    )
    assert (
        client.put(
            base + "/connection/token",
            json={"revision": 1, "token": "secret", "path": "/tmp/secret"},
        ).status_code
        == 422
    )
    assert client.post(base + "/oauth/start").status_code == 400
    assert (
        client.get(
            base + "/oauth/launch",
            params={"ticket": "forged"},
            headers={"Authorization": ""},
        ).status_code
        == 400
    )
    assert (
        "secret"
        not in client.put(
            base + "/connection/token", json={"revision": 1, "token": "bad secret"}
        ).text
    )


def test_instance_refresh_only_writes_selected_bundle(setup, monkeypatch):
    from openjarvis.connectors.instance_sources import refresh_instance

    client, manager, directory = setup
    record = source(setup, "spotify")
    destination = token_path(directory, record["id"])
    oauth.save_tokens(
        str(destination),
        {
            "access_token": "old",
            "refresh_token": "refresh",
            "client_id": "app",
            "client_secret": "secret",
            "expires_at": 0,
            "requested_scopes": ["user-read-recently-played"],
        },
    )
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(
            200,
            json={
                "access_token": "renewed",
                "refresh_token": "rotated",
                "expires_in": 3600,
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", post)
    refresh_instance(destination, "spotify")
    assert oauth.load_tokens(str(destination))["access_token"] == "renewed"
    assert oauth.load_tokens(str(destination))["refresh_token"] == "rotated"
    assert calls[0][1]["trust_env"] is False
    assert calls[0][1]["follow_redirects"] is False
    refresh_instance(destination, "spotify")
    assert len(calls) == 1


def test_reader_failure_stages_no_partial_documents_and_redacts_provider_error(
    setup, monkeypatch
):
    from openjarvis.connectors._stubs import Document

    client, manager, directory = setup
    record = source(setup, "oura")
    oauth.save_tokens(str(token_path(directory, record["id"])), {"token": "private"})
    reader = AccountSource("oura", record, directory)

    def scan(**kwargs):
        yield Document(doc_id="1", source="oura", doc_type="text", content="Partial")
        raise RuntimeError("provider reflected private")

    monkeypatch.setattr(reader.reader, "sync", scan)
    iterator = reader.sync()
    with pytest.raises(ValueError, match="Account sync failed") as error:
        next(iterator)
    assert "private" not in str(error.value)


def test_refresh_rejects_revocation_during_exchange(setup, monkeypatch):
    from openjarvis.connectors.instance_sources import refresh_instance
    from openjarvis.connectors.token_vault import TokenVault

    client, manager, directory = setup
    record = source(setup, "strava")
    destination = token_path(directory, record["id"])
    oauth.save_tokens(
        str(destination),
        {
            "access_token": "old",
            "refresh_token": "refresh",
            "client_id": "app",
            "client_secret": "secret",
            "requested_scopes": ["activity:read_all"],
        },
    )

    def revoked(url, **kwargs):
        TokenVault(destination).delete()
        return httpx.Response(
            200,
            json={"access_token": "stale", "expires_in": 3600},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", revoked)
    with pytest.raises(ValueError, match="refresh failed"):
        refresh_instance(destination, "strava")
    assert oauth.load_tokens(str(destination)) is None


def test_rotation_advances_revision_purges_previous_account_evidence(
    setup, monkeypatch
):
    from openjarvis.connectors._stubs import Document
    from openjarvis.connectors.oura import OuraConnector

    client, manager, directory = setup
    record = source(setup, "oura")

    def scan(self, **kwargs):
        yield Document(
            doc_id="1", source="oura", doc_type="text", content="Old account evidence"
        )

    monkeypatch.setattr(OuraConnector, "sync", scan)
    response = client.put(
        path(record) + "/connection/token", json={"revision": 1, "token": "first"}
    )
    assert response.status_code == 200
    assert manager.sync(record["id"]) == 1
    assert manager.list()[0]["chunks"] == 1
    stale = client.put(
        path(record) + "/connection/token", json={"revision": 1, "token": "second"}
    )
    assert stale.status_code == 409
    response = client.put(
        path(record) + "/connection/token", json={"revision": 2, "token": "second"}
    )
    assert response.status_code == 200
    assert manager.list()[0]["chunks"] == 0
    assert manager.store.get(record["id"])["revision"] == 3


def test_source_oauth_access_logs_redact_tickets_and_codes():
    import logging

    from openjarvis.server.oauth_logging import OAuthAccessFilter

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        "%s %s %s %s %s",
        (
            "client",
            "GET",
            "/v1/sources/abc/oauth/callback?state=private&code=secret",
            "HTTP",
            200,
        ),
        None,
    )
    assert OAuthAccessFilter().filter(record)
    assert "private" not in record.getMessage() and "secret" not in record.getMessage()


def test_removed_adapter_does_not_prevent_source_cleanup(setup, monkeypatch):
    from openjarvis.connectors import source_adapters

    client, manager, directory = setup
    record = source(setup, "oura")
    oauth.save_tokens(str(token_path(directory, record["id"])), {"token": "private"})
    monkeypatch.delitem(source_adapters._ADAPTERS, "oura_account")
    manager.delete(record["id"], record["revision"])
    assert oauth.load_tokens(str(token_path(directory, record["id"]))) is None
    with pytest.raises(KeyError):
        manager.store.get(record["id"])
