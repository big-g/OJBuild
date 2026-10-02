"""Pinned public destinations, bounded reads and redirect-policy enforcement."""

import socket
from unittest.mock import MagicMock

import httpx
import pytest

from openjarvis.security import public_http


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/",
        "http://127.0.0.1/",
        "https://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://100.100.100.200/",
        "http://192.168.1.10/",
        "http://100.64.0.1/",
        "http://metadata.google.internal/",
        "https://user:password@example.com/",
        "https://example.com/?api_key=secret",
        "https://example.com/?%74oken=secret",
        "https://example.com:8080/",
        "https://example.com/\nHost:other",
        "",
        None,
    ],
)
def test_public_configuration_rejects_private_or_credential_urls(url):
    with pytest.raises(ValueError):
        public_http.normalize_source_url(url)


def test_public_configuration_canonicalizes_host_and_fragment_without_fetching():
    assert (
        public_http.normalize_source_url("https://EXAMPLE.COM#section")
        == "https://example.com/"
    )
    assert (
        public_http.normalize_source_url("https://example.com/search?q=policy")
        == "https://example.com/search?q=policy"
    )


@pytest.mark.parametrize(
    "addresses",
    [
        ["93.184.216.34", "127.0.0.1"],
        ["100.64.0.2"],
        ["::ffff:10.0.0.1"],
        [],
    ],
)
def test_all_dns_answers_must_be_public(addresses, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
            for address in addresses
        ],
    )
    with pytest.raises(ValueError):
        public_http.resolve_public_addresses("example.test", 443)


def test_public_transport_pins_address_and_preserves_tls_hostname(monkeypatch):
    sock, tls = MagicMock(), MagicMock()
    context = MagicMock()
    context.wrap_socket.return_value = tls
    connect = MagicMock(return_value=sock)
    monkeypatch.setattr(socket, "create_connection", connect)
    connection = public_http.PinnedHTTPSConnection(
        "example.test", 443, pinned_ip="93.184.216.34", timeout=20, context=context
    )
    connection.connect()
    connect.assert_called_once_with(("93.184.216.34", 443), 20, None)
    context.wrap_socket.assert_called_once_with(sock, server_hostname="example.test")


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_provider_post_never_follows_redirects(monkeypatch, status):
    target = public_http.PublicTarget(
        "https",
        "api.notion.com",
        443,
        "/v1/search",
        "api.notion.com",
        ("93.184.216.34",),
    )
    monkeypatch.setattr(public_http, "validate_public_url", lambda url: target)
    request = MagicMock(
        return_value=httpx.Response(
            status,
            headers={"Location": "https://attacker.example/"},
        )
    )
    monkeypatch.setattr(public_http, "_request_source", request)
    with pytest.raises(ValueError, match="POST redirects"):
        public_http.fetch_public_source(
            "https://api.notion.com/v1/search",
            accept="application/json",
            authentication={
                "origin": "https://api.notion.com",
                "secret": "protected-token",
                "headers": {"Authorization": "Bearer protected-token"},
            },
            method="POST",
            body=b"{}",
        )
    assert request.call_count == 1
    assert request.call_args.kwargs["method"] == "POST"
    assert request.call_args.kwargs["body"] == b"{}"


@pytest.mark.parametrize(
    "method,body",
    [
        ("DELETE", None),
        ("PUT", b"{}"),
        ("GET", b"{}"),
        ("POST", "not-bytes"),
        ("POST", b"x" * 65537),
    ],
)
def test_provider_method_and_body_rejected_before_dns(method, body, monkeypatch):
    resolve = MagicMock()
    monkeypatch.setattr(public_http, "validate_public_url", resolve)
    with pytest.raises(ValueError, match="method or body"):
        public_http.fetch_public_source(
            "https://api.notion.com/v1/search",
            accept="application/json",
            method=method,
            body=body,
        )
    resolve.assert_not_called()


class Response:
    status = 200

    def __init__(self, body=b"hello", headers=None, declared_length=None):
        self.body = body
        self.headers = headers or {"Content-Type": "text/plain"}
        self.length = len(body) if declared_length is None else declared_length
        self.read_sizes = []

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def getheaders(self):
        return list(self.headers.items())

    def read1(self, limit):
        self.read_sizes.append(limit)
        chunk, self.body = self.body[:limit], self.body[limit:]
        self.length -= len(chunk)
        return chunk


def transport(monkeypatch, response):
    connection = MagicMock()
    connection.getresponse.return_value = response
    factory = MagicMock(return_value=connection)
    monkeypatch.setattr(public_http, "PinnedHTTPConnection", factory)
    target = public_http.PublicTarget(
        "http", "example.test", 80, "/page", "example.test", ("93.184.216.34",)
    )
    return connection, factory, target


def test_response_size_is_bounded_before_materializing_body(monkeypatch):
    response = Response(body=b"0123456789")
    connection, factory, target = transport(monkeypatch, response)
    with pytest.raises(ValueError, match="byte limit"):
        public_http._request_source(
            "http://example.test/page",
            target,
            max_bytes=5,
            deadline=public_http.time.monotonic() + 30,
            accept="text/plain",
        )
    assert response.read_sizes == [6]
    assert factory.call_args.kwargs["pinned_ip"] == "93.184.216.34"
    connection.close.assert_called_once()


def test_pinned_transport_sends_provider_post_body_and_preserves_host(monkeypatch):
    connection, _, target = transport(monkeypatch, Response(body=b"{}"))
    response = public_http._request_source(
        "http://example.test/page",
        target,
        max_bytes=100,
        deadline=public_http.time.monotonic() + 30,
        accept="application/json",
        method="POST",
        body=b'{"page_size":1}',
    )
    assert connection.request.call_args.args == ("POST", "/page")
    assert connection.request.call_args.kwargs["body"] == b'{"page_size":1}'
    assert connection.request.call_args.kwargs["headers"]["Host"] == "example.test"
    assert response.request.method == "POST"


@pytest.mark.parametrize(
    "response,error",
    [
        (Response(headers={"Content-Encoding": "gzip"}), "compressed"),
        (Response(body=b"partial", declared_length=100), "declared length"),
    ],
)
def test_compressed_and_truncated_responses_fail(response, error, monkeypatch):
    connection, _, target = transport(monkeypatch, response)
    with pytest.raises(ValueError, match=error):
        public_http._request_source(
            "http://example.test/page",
            target,
            max_bytes=100,
            deadline=public_http.time.monotonic() + 30,
            accept="text/plain",
        )
    connection.close.assert_called_once()


def test_deadline_and_proxy_variables_cannot_change_target(monkeypatch):
    connection, _, target = transport(monkeypatch, Response())
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    response = public_http._request_source(
        "http://example.test/page",
        target,
        max_bytes=100,
        deadline=public_http.time.monotonic() + 30,
        accept="text/plain",
    )
    assert response.content == b"hello"
    assert connection.request.call_args.kwargs["headers"]["Host"] == "example.test"
    assert "Authorization" not in connection.request.call_args.kwargs["headers"]
    with pytest.raises(ValueError, match="time budget"):
        public_http._remaining(public_http.time.monotonic() - 1)


def test_redirect_revalidates_and_pins_each_destination(monkeypatch):
    targets = []

    def validate(url):
        targets.append(url)
        return MagicMock()

    responses = iter(
        [
            httpx.Response(302, headers={"location": "/final"}),
            httpx.Response(
                200,
                content=b"readable",
                request=httpx.Request("GET", "https://example.test/final"),
            ),
        ]
    )
    monkeypatch.setattr(public_http, "validate_public_url", validate)
    monkeypatch.setattr(
        public_http, "_request_source", lambda *args, **kwargs: next(responses)
    )
    result = public_http.fetch_public_source(
        "https://example.test/start", accept="text/plain"
    )
    assert targets == ["https://example.test/start", "https://example.test/final"]
    assert str(result.url) == "https://example.test/final"


@pytest.mark.parametrize(
    "location",
    ["http://127.0.0.1/", "http://example.test/", "https://example.test/?token=secret"],
)
def test_redirect_blocks_private_downgrade_and_credential_targets(
    location, monkeypatch
):
    monkeypatch.setattr(public_http, "validate_public_url", lambda url: MagicMock())
    request = MagicMock(
        return_value=httpx.Response(302, headers={"location": location})
    )
    monkeypatch.setattr(public_http, "_request_source", request)
    with pytest.raises(ValueError):
        public_http.fetch_public_source(
            "https://example.test/start", accept="text/plain"
        )
    request.assert_called_once()


@pytest.mark.parametrize("status", [204, 206, 304, 401, 403, 404, 429, 500])
def test_noncomplete_or_failed_http_responses_are_errors(status, monkeypatch):
    monkeypatch.setattr(public_http, "validate_public_url", lambda url: MagicMock())
    monkeypatch.setattr(
        public_http, "_request_source", lambda *args, **kwargs: httpx.Response(status)
    )
    with pytest.raises(ValueError, match=f"HTTP {status}"):
        public_http.fetch_public_source("https://example.test/", accept="text/plain")


def test_redirect_count_is_bounded(monkeypatch):
    monkeypatch.setattr(public_http, "validate_public_url", lambda url: MagicMock())
    request = MagicMock(
        return_value=httpx.Response(302, headers={"location": "/again"})
    )
    monkeypatch.setattr(public_http, "_request_source", request)
    with pytest.raises(ValueError, match="five-redirect"):
        public_http.fetch_public_source("https://example.test/", accept="text/plain")
    assert request.call_count == 6


def test_fetch_pins_the_single_validated_dns_answer(monkeypatch):
    import openjarvis.security.ssrf as ssrf

    connection, factory, _ = transport(monkeypatch, Response())
    monkeypatch.setattr(ssrf, "check_ssrf", lambda url: None)
    resolve = MagicMock(
        side_effect=[
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))],
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))],
        ]
    )
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    public_http.fetch_public_source("http://example.test/page", accept="text/plain")
    assert resolve.call_count == 1
    assert factory.call_args.kwargs["pinned_ip"] == "93.184.216.34"
    assert connection.request.call_args.kwargs["headers"]["Host"] == "example.test"


@pytest.mark.parametrize(
    "destination",
    [
        "https://other.example.com/data",
        "http://api.example.com/data",
        "https://api.example.com:80/data",
        "https://127.0.0.1/data",
    ],
)
def test_authenticated_redirect_rejected_before_second_request(
    monkeypatch, destination
):
    requests = []
    monkeypatch.setattr(public_http, "validate_public_url", lambda url: object())

    def request(url, target, **kwargs):
        requests.append((url, kwargs))
        return httpx.Response(
            302, headers={"location": destination}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(public_http, "_request_source", request)
    with pytest.raises(ValueError):
        public_http.fetch_public_source(
            "https://api.example.com/start",
            accept="application/json",
            authentication={
                "origin": "https://api.example.com",
                "headers": {"Authorization": "Bearer protected"},
            },
        )
    assert len(requests) == 1
    assert requests[0][1]["credential_headers"] == {"Authorization": "Bearer protected"}


def test_same_origin_authenticated_redirect_preserves_header(monkeypatch):
    requests = []
    monkeypatch.setattr(public_http, "validate_public_url", lambda url: object())

    def request(url, target, **kwargs):
        requests.append(kwargs["credential_headers"])
        if len(requests) == 1:
            return httpx.Response(
                302, headers={"location": "/final"}, request=httpx.Request("GET", url)
            )
        return httpx.Response(200, content=b"{}", request=httpx.Request("GET", url))

    monkeypatch.setattr(public_http, "_request_source", request)
    result = public_http.fetch_public_source(
        "https://api.example.com/start",
        accept="application/json",
        authentication={
            "origin": "https://api.example.com",
            "headers": {"X-API-Key": "protected"},
        },
    )
    assert str(result.url) == "https://api.example.com/final"
    assert requests == [{"X-API-Key": "protected"}] * 2


def test_pinned_transport_sends_credential_as_header(monkeypatch):
    connection, _factory, target = transport(monkeypatch, Response(b"{}"))
    public_http._request_source(
        "https://example.test/data",
        target,
        max_bytes=100,
        deadline=public_http.time.monotonic() + 60,
        accept="application/json",
        credential_headers={"Authorization": "Bearer protected"},
    )
    headers = connection.request.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer protected"
    assert headers["Host"] == target.host_header


def test_authenticated_redirect_cannot_reflect_secret_into_url(monkeypatch):
    requests = []
    monkeypatch.setattr(public_http, "validate_public_url", lambda url: object())

    def request(url, target, **kwargs):
        requests.append(url)
        return httpx.Response(
            302,
            headers={"location": "/echo/protected-secret"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(public_http, "_request_source", request)
    with pytest.raises(ValueError, match="reflects a credential"):
        public_http.fetch_public_source(
            "https://api.example.com/start",
            accept="application/json",
            authentication={
                "origin": "https://api.example.com",
                "headers": {"Authorization": "Bearer protected-secret"},
                "secret": "protected-secret",
            },
        )
    assert requests == ["https://api.example.com/start"]


def test_pagination_origin_blocks_redirect_before_second_request(monkeypatch):
    requests = []
    monkeypatch.setattr(public_http, "validate_public_url", lambda url: object())

    def request(url, target, **kwargs):
        requests.append(url)
        return httpx.Response(
            302,
            headers={"location": "https://other.example.com/data"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(public_http, "_request_source", request)
    with pytest.raises(ValueError, match="configured origin"):
        public_http.fetch_public_source(
            "https://api.example.com/start",
            accept="application/json",
            allowed_origin="https://api.example.com:443",
        )
    assert len(requests) == 1


def test_fetch_respects_shared_deadline_before_dns_or_connection(monkeypatch):
    dns = MagicMock()
    monkeypatch.setattr(public_http, "validate_public_url", dns)
    with pytest.raises(ValueError, match="time budget"):
        public_http.fetch_public_source(
            "https://api.example.com/data",
            accept="application/json",
            deadline=public_http.time.monotonic() - 1,
        )
    dns.assert_not_called()


def test_cancelled_source_fetch_stops_before_dns(monkeypatch):
    from openjarvis.connectors.sync_control import SyncCancelled

    event = MagicMock()
    event.is_set.return_value = True
    dns = MagicMock()
    monkeypatch.setattr(public_http, "validate_public_url", dns)
    with pytest.raises(SyncCancelled):
        public_http.fetch_public_source(
            "https://api.example.com/data",
            accept="application/json",
            cancel_event=event,
        )
    dns.assert_not_called()


def test_pinned_fetch_cancellation_between_reads_closes_connection(monkeypatch):
    from openjarvis.connectors.sync_control import SyncCancelled

    response = Response(body=b"hello")
    connection, _, target = transport(monkeypatch, response)
    event = MagicMock()
    event.is_set.side_effect = [False, False, True]
    with pytest.raises(SyncCancelled):
        public_http._request_source(
            "https://example.test/data",
            target,
            max_bytes=100,
            deadline=public_http.time.monotonic() + 60,
            accept="text/plain",
            cancel_event=event,
        )
    connection.close.assert_called_once()


def test_weather_query_secret_is_injected_only_into_pinned_wire_target(monkeypatch):
    target = public_http.PublicTarget(
        "https",
        "api.openweathermap.org",
        443,
        "/data/2.5/weather?q=Boston",
        "api.openweathermap.org",
        ("93.184.216.34",),
    )
    connection = MagicMock()
    connection.sock = None
    response = MagicMock()
    response.status = 200
    response.length = 0
    response.getheader.return_value = "identity"
    response.getheaders.return_value = []
    response.read1.side_effect = [b"{}", b""]
    connection.getresponse.return_value = response
    monkeypatch.setattr(
        public_http, "PinnedHTTPSConnection", lambda *args, **kwargs: connection
    )
    value = public_http._request_source(
        "https://api.openweathermap.org/data/2.5/weather?q=Boston",
        target,
        max_bytes=100,
        deadline=public_http.time.monotonic() + 10,
        accept="application/json",
        credential_query={"appid": "protected-weather-key"},
    )
    assert (
        connection.request.call_args.args[1]
        == "/data/2.5/weather?q=Boston&appid=protected-weather-key"
    )
    assert "protected-weather-key" not in str(value.url)


@pytest.mark.parametrize(
    "origin,query",
    [
        ("https://attacker.example", {"appid": "secret"}),
        ("https://api.openweathermap.org", {"token": "secret"}),
        ("https://api.openweathermap.org", {"appid": "different"}),
    ],
)
def test_query_authentication_is_restricted_to_trusted_weather_adapter(
    monkeypatch, origin, query
):
    def forbidden(*args, **kwargs):
        raise AssertionError("No DNS or HTTP before validating query authentication")

    monkeypatch.setattr(public_http, "validate_public_url", forbidden)
    with pytest.raises(ValueError):
        public_http.fetch_public_source(
            origin + "/",
            accept="application/json",
            authentication={
                "origin": origin,
                "secret": "secret",
                "headers": {},
                "query": query,
            },
        )


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_weather_query_auth_never_follows_redirects(monkeypatch, status):
    target = public_http.PublicTarget(
        "https",
        "api.openweathermap.org",
        443,
        "/data/2.5/weather?q=Boston",
        "api.openweathermap.org",
        ("93.184.216.34",),
    )
    monkeypatch.setattr(public_http, "validate_public_url", lambda url: target)
    request = MagicMock(
        return_value=httpx.Response(
            status, headers={"Location": "https://api.openweathermap.org/other"}
        )
    )
    monkeypatch.setattr(public_http, "_request_source", request)
    with pytest.raises(ValueError, match="redirects"):
        public_http.fetch_public_source(
            "https://api.openweathermap.org/data/2.5/weather?q=Boston",
            accept="application/json",
            authentication={
                "origin": "https://api.openweathermap.org",
                "secret": "protected-key",
                "headers": {},
                "query": {"appid": "protected-key"},
            },
        )
    assert request.call_count == 1
    assert request.call_args.kwargs["credential_query"] == {"appid": "protected-key"}
