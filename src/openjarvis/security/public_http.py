"""Public HTTP targets and pinned connections shared by source adapters.

DNS validation is followed by IP pinning while retaining HTTP Host and TLS SNI.
Authenticated redirects carry only the bound header on the same HTTPS origin.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urldefrag, urljoin, urlparse

import httpx


@dataclass(frozen=True)
class PublicTarget:
    """A validated URL plus the exact public addresses it may connect to."""

    scheme: str
    hostname: str
    port: int
    request_target: str
    host_header: str
    addresses: tuple[str, ...]


def resolve_public_addresses(hostname: str, port: int) -> tuple[str, ...]:
    """Resolve once, reject every non-global answer, and return pinned IPs."""
    from openjarvis.security.ssrf import is_private_ip

    try:
        results = socket.getaddrinfo(
            hostname,
            port,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ValueError(f"Source hostname could not be resolved: {hostname}") from exc

    addresses: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in results:
        raw_ip = sockaddr[0].split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise ValueError(f"Source resolved to an invalid IP: {raw_ip}") from exc
        # ``is_global`` closes gaps outside the project's original RFC1918
        # list (CGNAT, benchmarking, documentation, and other special ranges).
        if not address.is_global or is_private_ip(str(address)):
            raise ValueError(f"Source resolved to a non-public IP: {address}")
        normalized = str(address)
        if normalized not in addresses:
            addresses.append(normalized)

    if not addresses:
        raise ValueError(f"Source hostname returned no addresses: {hostname}")
    return tuple(addresses)


def validate_public_url(url: str, *, resolver=None) -> PublicTarget:
    """Reject malformed/private targets and pin the addresses just checked."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Source must be an http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Source URLs must not contain user credentials")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Source must include a hostname")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("Source URL contains an invalid port") from exc

    from openjarvis.security.ssrf import check_ssrf

    error = check_ssrf(url)
    if error:
        raise ValueError(f"Source URL is not allowed: {error}")

    # IDNA is used consistently for DNS, HTTP Host, TLS SNI, and certificate
    # verification. IP literals are already ASCII and remain unchanged.
    try:
        ipaddress.ip_address(hostname)
        connection_hostname = hostname
    except ValueError:
        try:
            connection_hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("Source hostname is not valid IDNA") from exc

    addresses = (resolver or resolve_public_addresses)(connection_hostname, port)
    default_port = 443 if parsed.scheme == "https" else 80
    displayed_host = (
        f"[{connection_hostname}]"
        if ":" in connection_hostname
        else connection_hostname
    )
    host_header = displayed_host if port == default_port else f"{displayed_host}:{port}"
    request_target = parsed.path or "/"
    if parsed.query:
        request_target = f"{request_target}?{parsed.query}"

    return PublicTarget(
        scheme=parsed.scheme,
        hostname=connection_hostname,
        port=port,
        request_target=request_target,
        host_header=host_header,
        addresses=addresses,
    )


class _PinnedConnectionMixin:
    """Connect to a verified IP while retaining the URL host for HTTP/TLS."""

    def __init__(self, host: str, port: int, *, pinned_ip: str, **kwargs: Any) -> None:
        self._pinned_ip = pinned_ip
        super().__init__(host, port, **kwargs)
        # HTTPConnection.__init__ deliberately installs socket.create_connection
        # as an instance attribute, so replace that hook after initialization.
        self._create_connection = self._create_pinned_connection

    def _create_pinned_connection(self, address, timeout, source_address):
        # Ignore ``address[0]`` so http.client never performs a second DNS
        # lookup. HTTPSConnection still uses ``self.host`` as TLS SNI and for
        # certificate hostname verification.
        return socket.create_connection(
            (self._pinned_ip, address[1]),
            timeout,
            source_address,
        )


class PinnedHTTPConnection(_PinnedConnectionMixin, http.client.HTTPConnection):
    pass


class PinnedHTTPSConnection(_PinnedConnectionMixin, http.client.HTTPSConnection):
    pass


_SECRET_QUERY_KEYS = {
    "key",
    "apikey",
    "api_key",
    "token",
    "access_token",
    "authorization",
    "password",
    "secret",
    "signature",
    "sig",
    "credential",
    "credentials",
}


def normalize_source_url(url: str) -> str:
    """Validate public-source URL syntax without doing I/O at configuration time."""
    if not isinstance(url, str) or not url.strip() or len(url) > 4096:
        raise ValueError("A public http(s) URL of at most 4096 characters is required")
    if any(ord(char) < 33 or ord(char) == 127 for char in url.strip()):
        raise ValueError("URL contains whitespace or control characters")
    url = urldefrag(url.strip())[0]
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("A public http(s) URL is required")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Public source URLs must not contain credentials")
    port = parsed.port
    if port is not None and port not in {80, 443}:
        raise ValueError("Public sources support only ports 80 and 443")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname in {"localhost", "metadata.google.internal", "metadata.google.com"}:
        raise ValueError("Private source URLs are not allowed")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        hostname = hostname.encode("idna").decode("ascii")
    else:
        from openjarvis.security.ssrf import is_private_ip

        if not address.is_global or is_private_ip(str(address)):
            raise ValueError("Private source URLs are not allowed")
    if any(key.lower() in _SECRET_QUERY_KEYS for key, _ in parse_qsl(parsed.query)):
        raise ValueError("Public sources do not accept credential query parameters")
    displayed_host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        displayed_host += f":{port}"
    return parsed._replace(
        netloc=displayed_host, path=parsed.path or "/", fragment=""
    ).geturl()


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("Source fetch exceeded its time budget")
    return min(20.0, remaining)


def _request_source(
    url: str,
    target: PublicTarget,
    *,
    max_bytes: int,
    deadline: float,
    accept: str,
    credential_headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Read a bounded, uncompressed response from verified public addresses."""
    last_error = None
    for address in target.addresses[:4]:
        connection_cls = (
            PinnedHTTPSConnection if target.scheme == "https" else PinnedHTTPConnection
        )
        connection = connection_cls(
            target.hostname,
            target.port,
            pinned_ip=address,
            timeout=_remaining(deadline),
        )
        try:
            connection.request(
                "GET",
                target.request_target,
                headers={
                    "Host": target.host_header,
                    "Accept": accept,
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                    "User-Agent": "OpenJarvis-Sources/1.0",
                    **(credential_headers or {}),
                },
            )
            response = connection.getresponse()
            # Redirects have no ingestion body. Do not download arbitrary large
            # error pages when a status code is already sufficient to fail.
            if response.status != 200:
                return httpx.Response(
                    response.status,
                    headers=response.getheaders(),
                    request=httpx.Request("GET", url),
                )
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise ValueError("Source returned unsupported compressed content")
            data = bytearray()
            while True:
                if connection.sock is not None:
                    connection.sock.settimeout(_remaining(deadline))
                chunk = response.read1(min(65536, max_bytes + 1 - len(data)))
                if not chunk:
                    if response.length not in (None, 0):
                        raise ValueError(
                            "Source response ended before its declared length"
                        )
                    break
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ValueError(
                        f"Source response exceeds the {max_bytes}-byte limit"
                    )
                _remaining(deadline)
            return httpx.Response(
                response.status,
                headers=response.getheaders(),
                content=bytes(data),
                request=httpx.Request("GET", url),
            )
        except (OSError, http.client.HTTPException) as exc:
            last_error = exc
        finally:
            connection.close()
    raise ValueError("Source connection failed or timed out") from last_error


def source_origin(url: str) -> str:
    """Canonical origin for a normalized source URL, including effective port."""
    parsed = urlparse(normalize_source_url(url))
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme}://{parsed.hostname}:{port}"


def fetch_public_source(
    url: str,
    *,
    accept: str,
    max_bytes: int = 2 * 1024 * 1024,
    authentication: dict | None = None,
    deadline: float | None = None,
    allowed_origin: str | None = None,
) -> httpx.Response:
    """Validate and pin each redirect; reject partial/empty/error responses."""
    deadline = (
        min(deadline, time.monotonic() + 60)
        if deadline is not None
        else time.monotonic() + 60
    )
    current = normalize_source_url(url)
    for _ in range(6):
        _remaining(deadline)
        if authentication:
            from openjarvis.connectors.source_credentials import credential_origin

            secret = authentication.get("secret")
            if secret:
                from urllib.parse import quote, unquote

                if secret in unquote(current) or quote(secret, safe="") in current:
                    raise ValueError("Authenticated source URL reflects a credential")
            if credential_origin(current) != authentication["origin"]:
                raise ValueError(
                    "Authenticated redirects must stay on the credential HTTPS origin"
                )
        if allowed_origin is not None and source_origin(current) != allowed_origin:
            raise ValueError("Paginated redirects must stay on the configured origin")
        target = validate_public_url(current)
        response = _request_source(
            current,
            target,
            max_bytes=max_bytes,
            deadline=deadline,
            accept=accept,
            **(
                {"credential_headers": authentication["headers"]}
                if authentication
                else {}
            ),
        )
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            if not location:
                raise ValueError("Source redirect has no destination")
            redirected = normalize_source_url(urljoin(current, location))
            if (
                urlparse(current).scheme == "https"
                and urlparse(redirected).scheme != "https"
            ):
                raise ValueError("Source redirects must not downgrade HTTPS")
            current = redirected
            continue
        if response.status_code != 200:
            raise ValueError(f"Source returned HTTP {response.status_code}")
        return response
    raise ValueError("Source exceeded the five-redirect limit")
