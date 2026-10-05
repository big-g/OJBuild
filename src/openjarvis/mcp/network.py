"""Per-connection MCP destination and TLS policy; configuration performs no I/O."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from openjarvis.connectors.imap import _BLOCKED_HOSTNAMES, _normalize_hostname
from openjarvis.connectors.imap_network import (
    tls_context,
    validate_lan_endpoint,
    validate_network_config,
)
from openjarvis.security.public_http import (
    PublicTarget,
    normalize_source_url,
    validate_public_url,
)

NETWORK_FIELDS = {"network_access", "lan_addresses", "tls_trust", "ca_certificate"}


def policy(config):
    # Reuse the existing exact private-address and bounded PEM CA validation.
    try:
        result = validate_network_config(config)
    except ValueError:
        raise ValueError(
            "Invalid MCP network policy: use exact private IPs "
            "and valid CA certificates"
        ) from None
    if result["network_access"] == "public" and result["tls_trust"] != "system":
        raise ValueError(
            "Private CA trust requires an explicitly authorized LAN connection"
        )
    return result


def endpoint(url, config=None):
    settings = policy(config or {})
    if settings["network_access"] == "public":
        normalized = normalize_source_url(url)
        if not normalized.startswith("https://") or "?" in normalized:
            raise ValueError("Managed MCP requires a public HTTPS URL without a query")
        return normalized
    if (
        not isinstance(url, str)
        or not 1 <= len(url) <= 4096
        or any(ord(c) < 33 or ord(c) == 127 for c in url)
    ):
        raise ValueError("Invalid LAN MCP URL")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "?" in url
        or "#" in url
    ):
        raise ValueError(
            "LAN MCP requires HTTPS without credentials, queries or fragments"
        )
    host = _normalize_hostname(parsed.hostname)
    if host in _BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        raise ValueError("Localhost and metadata endpoints are not allowed")
    port = parsed.port if parsed.port is not None else 443
    if not 1 <= port <= 65535:
        raise ValueError("Invalid LAN MCP port")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if str(address) not in {
            value.strip() for value in settings["lan_addresses"].split(",")
        }:
            raise ValueError("LAN MCP IP must be explicitly authorized")
    displayed = f"[{host}]" if ":" in host else host
    if parsed.port is not None:
        displayed += f":{port}"
    return parsed._replace(netloc=displayed, path=parsed.path or "/").geturl()


def target(url, config):
    if config["network_access"] == "public":
        return validate_public_url(url)
    parsed = urlparse(url)
    port = parsed.port or 443
    host, addresses = validate_lan_endpoint(
        parsed.hostname, port, config["lan_addresses"]
    )
    displayed = f"[{host}]" if ":" in host else host
    return PublicTarget(
        "https",
        host,
        port,
        (parsed.path or "/") + (";" + parsed.params if parsed.params else ""),
        displayed if port == 443 else f"{displayed}:{port}",
        addresses,
    )


def context(config):
    return tls_context(config)
