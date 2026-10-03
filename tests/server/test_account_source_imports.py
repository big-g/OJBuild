"""Authenticated import transport exports metadata, never credential bundles."""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.connectors.instance_sources import ACCOUNT_READERS, token_path
from openjarvis.connectors.oauth import (
    connector_scopes,
    get_provider_for_connector,
    load_tokens,
    save_tokens,
)
from openjarvis.connectors.source_imports import _IMPORTS
from openjarvis.connectors.source_manager import SourceManager
from openjarvis.connectors.source_store import SourceStore
from openjarvis.server.auth_middleware import AuthMiddleware
from openjarvis.server.sources_router import create_sources_router


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("Imports must not contact a provider")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)
    manager = SourceManager(
        SourceStore(
            str(tmp_path / "sources.db"), legacy_path=str(tmp_path / "missing")
        ),
        knowledge_path=str(tmp_path / "knowledge.db"),
    )
    app = FastAPI()
    app.add_middleware(AuthMiddleware, api_key="test-key")
    app.include_router(create_sources_router(manager))
    with TestClient(app, headers={"Authorization": "Bearer test-key"}) as client:
        yield client, manager


@pytest.mark.parametrize("service", ACCOUNT_READERS)
def test_named_account_import_http_flow_is_authenticated_secret_free_and_replayable(
    setup, service
):
    client, manager = setup
    auth = ACCOUNT_READERS[service][3]
    if auth == "oauth":
        provider = get_provider_for_connector(service)
        tokens = {
            "access_token": "protected-access-token",
            "refresh_token": "protected-refresh-token",
            "client_id": "test-application",
            "client_secret": "protected-client-secret",
            "requested_scopes": connector_scopes(provider, service),
        }
    elif service == "weather":
        tokens = {"api_key": "protected-weather-key", "location": "Boston"}
    else:
        tokens = {
            "token": "xoxp-protected-user-token"
            if service == "slack"
            else "protected-account-token"
        }
    path = manager.store.path.parent / "connectors" / _IMPORTS[service].filename
    save_tokens(str(path), tokens)
    base = "/v1/sources/imports/" + service
    assert (
        client.post(
            base + "/preview", json={"name": "Work"}, headers={"Authorization": ""}
        ).status_code
        == 401
    )
    response = client.post(base + "/preview", json={"name": "Work"})
    assert response.status_code == 200, response.text
    plan = response.json()
    assert len(plan["plan_token"]) <= 4096
    assert plan["credential_storage"] == "bundle"
    assert (
        client.post(
            base, json={"plan_token": plan["plan_token"]}, headers={"Authorization": ""}
        ).status_code
        == 401
    )
    applied = client.post(base, json={"plan_token": plan["plan_token"]})
    assert applied.status_code == 200, applied.text
    assert (
        client.post(base, json={"plan_token": plan["plan_token"]}).json()
        == applied.json()
    )
    record = applied.json()
    assert (
        "legacy_document_ids" not in record and "credential_id" not in record["config"]
    )
    assert load_tokens(str(token_path(manager.store.path.parent, record["id"])))
    responses = [
        response,
        applied,
        client.get("/v1/sources"),
        client.get("/v1/sources/imports"),
        client.get("/v1/sources/credentials"),
        client.get("/v1/sources/audit"),
        client.get(f"/v1/sources/{record['id']}/connection"),
    ]
    assert all("protected-" not in result.text for result in responses)
    assert (
        client.post(
            base, json={"plan_token": plan["plan_token"], "token": "protected-extra"}
        ).status_code
        == 422
    )
    assert (
        "protected-extra"
        not in client.post(
            base, json={"plan_token": plan["plan_token"], "token": "protected-extra"}
        ).text
    )
    assert load_tokens(str(path)) == tokens


@pytest.mark.parametrize("service", ["imap", "gmail_imap"])
def test_imap_password_import_transport_exports_settings_only(setup, service):
    client, manager = setup
    values = {
        "email": "protected-user@example.com",
        "password": " protected-password ",
        "imap_host": "mail.example.com",
        "imap_port": 993,
        "imap_security": "tls",
    }
    path = manager.store.path.parent / "connectors" / _IMPORTS[service].filename
    save_tokens(str(path), values)
    base = "/v1/sources/imports/" + service
    assert (
        client.post(
            base + "/preview", json={"name": "Mail"}, headers={"Authorization": ""}
        ).status_code
        == 401
    )
    response = client.post(base + "/preview", json={"name": "Mail"})
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["connection_auth"] == "password"
    assert plan["config"]["host"] == "mail.example.com"
    assert len(plan["plan_token"]) <= 4096
    assert (
        client.post(
            base, json={"plan_token": plan["plan_token"]}, headers={"Authorization": ""}
        ).status_code
        == 401
    )
    applied = client.post(base, json={"plan_token": plan["plan_token"]})
    assert applied.status_code == 200, applied.text
    source = applied.json()
    assert source["adapter_id"] == "imap_account"
    assert load_tokens(str(token_path(manager.store.path.parent, source["id"]))) == {
        "username": values["email"],
        "password": values["password"],
    }
    assert load_tokens(str(path)) == values
    for result in [
        response,
        applied,
        client.get("/v1/sources"),
        client.get("/v1/sources/imports"),
        client.get("/v1/sources/audit"),
        client.get(f"/v1/sources/{source['id']}/connection"),
    ]:
        assert "protected-" not in result.text
