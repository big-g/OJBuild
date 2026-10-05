"""Offline LAN MCP guards, TLS trust, pinning and approval/credential invalidation."""

import json
import ssl
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.mcp.network import endpoint, policy
from openjarvis.mcp.protocol import MCPRequest
from openjarvis.mcp.runtime_manager import RuntimeMCPManager
from openjarvis.mcp.runtime_store import definition, fingerprint
from openjarvis.mcp.runtime_transport import RuntimeHTTPTransport
from openjarvis.server.auth_store import AuthStore
from openjarvis.server.runtime_mcp_router import create_runtime_mcp_router
from tests.connectors.test_imap_network import certificates as generate_certificates
from tests.connectors.test_imap_network import handshake


@pytest.fixture
def certificates():
    return generate_certificates.__wrapped__()


LAN = {"network_access": "lan", "lan_addresses": "192.168.1.20, fd00::20"}


def dns(monkeypatch, *addresses):
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args: [(2, 1, 6, "", (ip, args[1])) for ip in addresses],
    )


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "169.254.169.254",
        "fe80::1",
        "100.64.1.1",
        "8.8.8.8",
        "192.168.1.0/24",
        "::ffff:192.168.1.20",
        "0.0.0.0",
    ],
)
def test_exact_private_ranges_only(address):
    with pytest.raises(ValueError):
        policy({"network_access": "lan", "lan_addresses": address})


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.internal:8443/mcp",
        "https://localhost/mcp",
        "https://metadata.google.internal/mcp",
        "https://user:pass@mcp.internal/mcp",
        "https://mcp.internal/mcp?token=secret",
        "https://mcp.internal/mcp#x",
        "https://mcp.internal:0/mcp",
        "https://192.168.1.21/mcp",
    ],
)
def test_lan_url_requires_exact_https_endpoint(url):
    with pytest.raises(ValueError):
        endpoint(url, LAN)


def test_config_does_not_resolve_or_relax_public_default(monkeypatch):
    resolver = MagicMock(side_effect=AssertionError("Configuration must not resolve"))
    monkeypatch.setattr("socket.getaddrinfo", resolver)
    assert (
        endpoint("https://MCP.internal.:8443/mcp", LAN)
        == "https://mcp.internal:8443/mcp"
    )
    assert endpoint("https://[fd00::20]:8443/mcp", LAN) == "https://[fd00::20]:8443/mcp"
    resolver.assert_not_called()
    with pytest.raises(ValueError):
        endpoint("https://192.168.1.20/mcp")
    assert definition({"name": "old", "url": "https://example.com/mcp"}) == {
        "name": "old",
        "url": "https://example.com/mcp",
        "allow_without_confirmation": False,
    }


@pytest.mark.parametrize(
    "addresses",
    [
        ("192.168.1.21",),
        ("192.168.1.20", "8.8.8.8"),
        ("192.168.1.20", "127.0.0.1"),
        ("192.168.1.20", "169.254.169.254"),
        (),
    ],
)
def test_unlisted_mixed_or_empty_dns_fails_before_credentials(monkeypatch, addresses):
    dns(monkeypatch, *addresses)
    post = MagicMock()
    monkeypatch.setattr("openjarvis.mcp.runtime_transport._request_source", post)
    transport = RuntimeHTTPTransport(
        "https://mcp.internal:8443/mcp", "TEST-SECRET", network=LAN
    )
    with pytest.raises(ValueError):
        transport.send(MCPRequest(method="tools/list", id=1))
    post.assert_not_called()


def test_rebinding_revalidated_each_request_and_no_post_retry(monkeypatch):
    dns(monkeypatch, "192.168.1.20", "fd00::20")
    post = MagicMock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
    )
    monkeypatch.setattr("openjarvis.mcp.runtime_transport._request_source", post)
    transport = RuntimeHTTPTransport(
        "https://mcp.internal:8443/mcp", "TEST-SECRET", network=LAN
    )
    transport.send(MCPRequest(method="tools/list", id=1))
    pinned = post.call_args.args[1]
    assert (
        pinned.hostname == "mcp.internal" and pinned.host_header == "mcp.internal:8443"
    )
    assert pinned.addresses == ("192.168.1.20",) and pinned.port == 8443
    ctx = post.call_args.kwargs["ssl_context"]
    assert ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED
    dns(monkeypatch, "192.168.1.99")
    with pytest.raises(ValueError):
        transport.send(MCPRequest(method="tools/call", id=1))
    assert post.call_count == 1
    dns(monkeypatch, "192.168.1.20")
    post.side_effect = OSError("response lost")
    with pytest.raises(OSError):
        transport.send(MCPRequest(method="tools/call", id=1))
    assert post.call_count == 2


def test_mcp_private_ca_context_verifies_root_hostname_and_scope(
    tmp_path, monkeypatch, certificates
):
    root, leaf, key = certificates
    config = {**LAN, "tls_trust": "custom_ca", "ca_certificate": root}
    transport = RuntimeHTTPTransport("https://mail.internal:8443/mcp", network=config)
    monkeypatch.setattr(
        "tests.connectors.test_imap_network.tls_context",
        lambda _: transport._ssl_context,
    )
    handshake(tmp_path, config, leaf, key, "mail.internal")
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(tmp_path, config, leaf, key, "wrong.internal")
    normal = RuntimeHTTPTransport("https://mail.internal:8443/mcp", network=LAN)
    monkeypatch.setattr(
        "tests.connectors.test_imap_network.tls_context", lambda _: normal._ssl_context
    )
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(tmp_path, LAN, leaf, key, "mail.internal")
    for pem in [leaf.decode(), key.decode(), root + key.decode(), "", root * 33]:
        with pytest.raises(ValueError):
            policy({**LAN, "tls_trust": "custom_ca", "ca_certificate": pem})


def test_store_network_edit_invalidates_catalog_approval_and_retained_token(tmp_path):
    manager = RuntimeMCPManager(tmp_path / "mcp.db")
    config = {"name": "lan", "url": "https://mcp.internal:8443/mcp", **LAN}
    row = manager.store.create(config, "user:admin", "TEST-SECRET")
    old_digest = fingerprint(row)
    row = manager.store.change(
        row["id"],
        row["revision"],
        "discovered",
        "user:admin",
        catalog=[{"tool": "reviewed"}],
    )
    row = manager.store.change(row["id"], row["revision"], "approved", "user:admin")
    assert manager.store.approved(row)
    edited = manager.store.change(
        row["id"],
        row["revision"],
        "updated",
        "user:admin",
        config={**config, "lan_addresses": "192.168.1.21"},
    )
    assert (
        not edited["enabled"]
        and not edited["discovered"]
        and not manager.store.approved(edited)
    )
    assert edited["catalog"] == "[]" and manager.store.token(edited) == ""
    assert fingerprint(edited) != old_digest
    assert json.loads(edited["definition"])["lan_addresses"] == "192.168.1.21"
    # Persistence survives reopening; configuration is not a global bypass.
    assert RuntimeMCPManager(tmp_path / "mcp.db").store.get(row["id"]) == edited


def test_admin_api_round_trips_lan_settings_without_leaking_tokens(tmp_path):
    app = FastAPI()
    app.state.auth_store = AuthStore(tmp_path / "auth.db")
    for uid in ["admin", "user"]:
        app.state.auth_store.create_user(
            uid, uid, "password123", is_admin=uid == "admin"
        )
    manager = RuntimeMCPManager(tmp_path / "mcp.db")
    app.include_router(create_runtime_mcp_router(manager))
    c = TestClient(
        app,
        headers={"X-OpenJarvis-Session": app.state.auth_store.create_session("admin")},
    )
    config = {
        "name": "lan",
        "url": "https://mcp.internal:8443/mcp",
        "bearer_token": "TEST-SECRET",
        **LAN,
    }
    response = c.post("/v1/runtime-mcp", json=config)
    assert response.status_code == 201, response.text
    assert response.json()["network_access"] == "lan"
    assert not response.json()["approved"] and not response.json()["enabled"]
    assert "TEST-SECRET" not in response.text + c.get("/v1/runtime-mcp").text
    c.headers["X-OpenJarvis-Session"] = app.state.auth_store.create_session("user")
    assert c.post("/v1/runtime-mcp", json=config).status_code == 403


def test_context_reaches_pinned_tls_connection_without_second_dns(monkeypatch):
    import time

    from openjarvis.mcp.network import target
    from openjarvis.security import public_http

    dns(monkeypatch, "192.168.1.20")
    settings = policy(LAN)
    pinned = target("https://mcp.internal:8443/mcp", settings)
    ctx = ssl.create_default_context()
    connection = MagicMock()
    response = connection.getresponse.return_value
    response.status = 202
    response.getheaders.return_value = []
    constructor = MagicMock(return_value=connection)
    monkeypatch.setattr(public_http, "PinnedHTTPSConnection", constructor)
    public_http._request_source(
        "https://mcp.internal:8443/mcp",
        pinned,
        deadline=time.monotonic() + 10,
        max_bytes=1024,
        accept="application/json",
        method="POST",
        body=b"{}",
        ssl_context=ctx,
    )
    assert constructor.call_args.args == ("mcp.internal", 8443)
    assert constructor.call_args.kwargs["context"] is ctx
    assert constructor.call_args.kwargs["pinned_ip"] == "192.168.1.20"
    assert connection.request.call_args.kwargs["headers"]["Host"] == "mcp.internal:8443"


def test_manager_passes_saved_network_settings_to_transport(tmp_path, monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr("openjarvis.mcp.runtime_manager.RuntimeHTTPTransport", factory)
    client = MagicMock()
    monkeypatch.setattr(
        "openjarvis.mcp.runtime_manager.RuntimeMCPClient",
        MagicMock(return_value=client),
    )
    manager = RuntimeMCPManager(tmp_path / "manager.db")
    row = manager.store.create(
        {"name": "lan", "url": "https://mcp.internal:8443/mcp", **LAN},
        "admin",
        "TEST-SECRET",
    )
    with manager.session(row):
        pass
    assert factory.call_args.args == ("https://mcp.internal:8443/mcp", "TEST-SECRET")
    assert (
        factory.call_args.kwargs["network"]["lan_addresses"] == "192.168.1.20, fd00::20"
    )
    client.initialize.assert_called_once()
    client.close.assert_called_once()


def test_shared_transport_refuses_unverified_tls_context(monkeypatch):
    import time

    from openjarvis.security.public_http import PublicTarget, _request_source

    connection = MagicMock()
    monkeypatch.setattr(
        "openjarvis.security.public_http.PinnedHTTPSConnection", connection
    )
    pinned = PublicTarget(
        "https", "mcp.internal", 443, "/mcp", "mcp.internal", ("192.168.1.20",)
    )
    with pytest.raises(ValueError, match="verify certificates"):
        _request_source(
            "https://mcp.internal/mcp",
            pinned,
            deadline=time.monotonic() + 10,
            max_bytes=1024,
            accept="application/json",
            ssl_context=ssl._create_unverified_context(),
        )
    connection.assert_not_called()


def test_private_ca_cannot_relax_public_transport(certificates):
    root, _, _ = certificates
    with pytest.raises(ValueError, match="LAN connection"):
        policy({"tls_trust": "custom_ca", "ca_certificate": root})


def test_explicit_token_replacement_survives_network_edit(tmp_path):
    manager = RuntimeMCPManager(tmp_path / "replacement.db")
    config = {"name": "lan", "url": "https://mcp.internal:8443/mcp", **LAN}
    row = manager.store.create(config, "user:admin", "ORIGINAL-SECRET")
    row = manager.store.change(
        row["id"],
        row["revision"],
        "updated",
        "user:admin",
        config={**config, "lan_addresses": "192.168.1.21"},
        token="REPLACEMENT-SECRET",
    )
    assert manager.store.token(row) == "REPLACEMENT-SECRET"
    assert not row["enabled"] and not manager.store.approved(row)
    assert "REPLACEMENT-SECRET" not in json.dumps(manager.view(row))
