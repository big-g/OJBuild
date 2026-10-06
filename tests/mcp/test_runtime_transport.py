"""No live provider traffic: pinned targets, headers, sessions and protocol IDs."""

import json
from unittest.mock import MagicMock

import httpx
import pytest

from openjarvis.mcp.protocol import MCPRequest
from openjarvis.mcp.runtime_transport import RuntimeHTTPTransport
from openjarvis.security.public_http import PublicTarget


@pytest.fixture
def wire(monkeypatch):
    validate = MagicMock(
        return_value=PublicTarget(
            "https",
            "example.com",
            443,
            "/mcp",
            "example.com",
            ("8.8.8.8", "1.1.1.1"),
        )
    )
    request = MagicMock(
        return_value=httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"tools": []},
            },
            headers={"mcp-session-id": "session"},
        )
    )
    monkeypatch.setattr(
        "openjarvis.mcp.runtime_transport.validate_public_url", validate
    )
    monkeypatch.setattr("openjarvis.mcp.runtime_transport._request_source", request)
    return validate, request


def test_each_post_is_pinned_and_headers_follow_negotiation(wire):
    validate, post = wire
    transport = RuntimeHTTPTransport("https://example.com/mcp", "TEST-SECRET")
    assert transport.send(MCPRequest(method="tools/list", id=1)).result == {"tools": []}
    transport.protocol_version = "2025-11-25"
    transport.send(MCPRequest(method="tools/list", id=1))
    assert validate.call_count == 2

    kwargs = post.call_args.kwargs
    assert post.call_args.args[1].addresses == ("8.8.8.8",)
    assert kwargs["method"] == "POST" and kwargs["max_bytes"] == 2 * 1024 * 1024
    assert kwargs["credential_headers"] == {
        "Content-Type": "application/json",
        "Authorization": "Bearer TEST-SECRET",
        "Mcp-Session-Id": "session",
        "MCP-Protocol-Version": "2025-11-25",
    }
    assert json.loads(kwargs["body"])["method"] == "tools/list"
    transport.close()
    assert post.call_args.kwargs["method"] == "DELETE"
    assert post.call_args.kwargs["credential_headers"]["Mcp-Session-Id"] == "session"
    count = post.call_count
    transport.close()
    assert post.call_count == count
    assert transport._token == "" and transport._session is None
    with pytest.raises(ValueError, match="closed"):
        transport.send(MCPRequest(method="tools/list", id=1))
    assert validate.call_count == 2


def test_api_key_header_on_requests_notifications_and_cleanup(wire):
    validate, post = wire
    transport = RuntimeHTTPTransport(
        "https://example.com/mcp",
        "TEST-SECRET",
        auth={"auth_type": "api_key", "api_key_header": "X-API-Key"},
    )
    transport.send(MCPRequest(method="tools/list", id=1))
    headers = post.call_args.kwargs["credential_headers"]
    assert headers["x-api-key"] == "TEST-SECRET" and "Authorization" not in headers
    transport.send_notification(MCPRequest(method="notifications/initialized", id=None))
    assert post.call_args.kwargs["credential_headers"]["x-api-key"] == "TEST-SECRET"
    assert validate.call_count == 2
    transport.close()
    assert post.call_args.kwargs["method"] == "DELETE"
    assert post.call_args.kwargs["credential_headers"]["x-api-key"] == "TEST-SECRET"
    assert transport._token == ""


@pytest.mark.parametrize(
    "header",
    [
        "Authorization",
        "HOST",
        "Cookie",
        "Content-Type",
        "Mcp-Session-Id",
        "mcp-protocol-version",
        "Proxy-Authorization",
        "X-Forwarded-Host",
        "Sec-Fetch-Site",
        "X-HTTP-Method-Override",
        "",
        "bad header",
        "X-Key\r\nHost",
        "1key",
        "x" * 65,
    ],
)
def test_api_key_header_cannot_override_protocol_or_inject(wire, header):
    _, post = wire
    with pytest.raises(ValueError):
        RuntimeHTTPTransport(
            "https://example.com/mcp",
            "TEST-SECRET",
            auth={"auth_type": "api_key", "api_key_header": header},
        )
    post.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"jsonrpc": "2.0", "id": 2, "result": {}},
        {"jsonrpc": "2.0", "id": True, "result": {}},
        {"jsonrpc": "1.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {}},
        {"jsonrpc": "2.0", "id": 1, "error": "TEST-SECRET"},
    ],
)
def test_malformed_or_mismatched_responses_fail(wire, payload):
    _, post = wire
    post.return_value = httpx.Response(200, json=payload)
    with pytest.raises(ValueError):
        RuntimeHTTPTransport("https://example.com/mcp").send(
            MCPRequest(method="tools/list", id=1)
        )


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 500])
def test_redirects_and_errors_never_follow_or_echo(wire, status):
    _, post = wire
    post.return_value = httpx.Response(
        status, text="TEST-SECRET", headers={"location": "https://other.com"}
    )
    with pytest.raises(ValueError) as error:
        RuntimeHTTPTransport("https://example.com/mcp").send(
            MCPRequest(method="tools/list", id=1)
        )
    assert post.call_count == 1 and "TEST-SECRET" not in str(error.value)


def test_notification_202_and_sse_json(wire):
    _, post = wire
    post.return_value = httpx.Response(202)
    transport = RuntimeHTTPTransport("https://example.com/mcp")
    transport.send_notification(MCPRequest(method="notifications/initialized", id=None))
    with pytest.raises(ValueError, match="no response"):
        transport.send(MCPRequest(method="tools/list", id=1))
    post.return_value = httpx.Response(
        200,
        text='event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n\n',
        headers={"content-type": "text/event-stream"},
    )
    assert transport.send(MCPRequest(method="tools/list", id=1)).result == {"tools": []}


def test_private_dns_rejected_before_post(monkeypatch):
    post = MagicMock()
    monkeypatch.setattr("openjarvis.mcp.runtime_transport._request_source", post)
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args: [
            (2, 1, 6, "", ("10.0.0.1", 443)),
        ],
    )
    with pytest.raises(ValueError, match="non-public"):
        RuntimeHTTPTransport("https://example.com/mcp", "TEST-SECRET").send(
            MCPRequest(method="tools/list", id=1)
        )
    post.assert_not_called()
