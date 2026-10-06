"""Validated authentication settings; credentials never belong in definitions."""

import re

from openjarvis.connectors.source_credentials import _RESERVED

AUTH_FIELDS = {"auth_type", "api_key_header"}


def authentication(value):
    mode = value.get("auth_type", "bearer")
    header = value.get("api_key_header", "")
    if not isinstance(mode, str) or mode not in {"bearer", "api_key"}:
        raise ValueError("Choose Bearer token or API key header authentication")
    if mode == "bearer":
        if header != "":
            raise ValueError("API key header applies only to API key authentication")
        return {"auth_type": "bearer", "api_key_header": ""}
    if not isinstance(header, str) or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9-]{0,63}", header
    ):
        raise ValueError(
            "API key header must contain 1–64 ASCII letters, digits or hyphens "
            "and start with a letter. Example: X-API-Key."
        )
    header = header.lower()
    if header in _RESERVED | {
        "content-type",
        "expect",
        "range",
        "origin",
        "referer",
        "forwarded",
        "via",
        "cache-control",
        "pragma",
        "mcp-session-id",
        "mcp-protocol-version",
    } or header.startswith(
        (
            "proxy-",
            "sec-",
            "mcp-",
            "content-",
            "accept-",
            "x-forwarded-",
            "x-http-method",
        )
    ):
        raise ValueError(
            "API key header cannot override HTTP routing, transport or MCP "
            "headers; example: X-API-Key"
        )
    return {"auth_type": "api_key", "api_key_header": header}
