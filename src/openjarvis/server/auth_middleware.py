"""API key authentication middleware for the OpenJarvis server."""

from __future__ import annotations

import base64
import logging
import os
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_WS_AUTH_PROTOCOL = "openjarvis.auth.v1"
_WS_KEY_PROTOCOL_PREFIX = "openjarvis.key.b64url."
_WS_SESSION_PROTOCOL = "openjarvis.session.v1"
_WS_SESSION_PROTOCOL_PREFIX = "openjarvis.session.b64url."

def _api_keys_match(presented: str, expected: str) -> bool:
    """Compare API keys as bytes so non-ASCII values do not raise ``TypeError``."""
    try:
        presented_bytes = presented.encode("utf-8")
        expected_bytes = expected.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return secrets.compare_digest(presented_bytes, expected_bytes)


class AuthMiddleware(BaseHTTPMiddleware):
    """Validates ``Authorization: Bearer <key>`` on ``/v1/*`` and ``/api/*`` routes.

    Webhook routes and health checks are exempt — they use
    per-channel signature verification instead.
    """

    def __init__(self, app, api_key: str = "") -> None:  # noqa: ANN001
        super().__init__(app)
        self._api_key = api_key or os.environ.get("OPENJARVIS_API_KEY", "")

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        # Browser CORS preflights never carry Authorization, and rejecting
        # them here means they never reach CORSMiddleware to get
        # Access-Control-Allow-* headers.
        if self._is_cors_preflight(request):
            return await call_next(request)

        if self._api_key and self._requires_auth(request.url.path):
            # Human session authentication is an alternative to the
            # master server API key for normal authenticated API routes.
            session_token = request.headers.get(
                "X-OpenJarvis-Session", ""
            ).strip()

            if session_token:
                from openjarvis.server.auth_store import AuthStore

                store = getattr(request.app.state, "auth_store", None)
                if store is None:
                    store = AuthStore()
                    request.app.state.auth_store = store

                user = store.get_user_for_token(session_token)
                if user is not None:
                    request.state.auth_user_id = str(user["user_id"])
                    request.state.auth_user = user
                    return await call_next(request)

            # Fall back to the existing master API-key authentication.
            auth = request.headers.get("Authorization", "")
            if not auth:
                return JSONResponse(
                    {"detail": "Missing Authorization header"},
                    status_code=401,
                )

            scheme, _, token = auth.partition(" ")

            if (
                scheme.lower() != "bearer"
                or not _api_keys_match(token, self._api_key)
            ):
                return JSONResponse(
                    {"detail": "Invalid API key"},
                    status_code=401,
                )

        return await call_next(request)    
    @staticmethod
    def _is_cors_preflight(request: Request) -> bool:
        return (
            request.method == "OPTIONS"
            and bool(request.headers.get("Origin"))
            and bool(request.headers.get("Access-Control-Request-Method"))
        )

    @staticmethod
    def _requires_auth(path: str) -> bool:
        """Protect API routes and operational metrics; leave auth login open.

        ``/v1/auth/login`` must be reachable without the master API key so
        normal users can exchange their username/password for a session token.
        Other ``/v1`` routes remain protected by the server API key.
        """
        if path in {"/v1/auth/login", "/v1/auth/logout", "/v1/auth/me"}:
            return False

        return (
            path.startswith("/v1/")
            or path.startswith("/api/")
            or path == "/metrics"
            or path.startswith("/metrics/")
        )

def generate_api_key() -> str:
    """Generate a new API key with ``oj_sk_`` prefix."""
    return f"oj_sk_{secrets.token_urlsafe(32)}"


def check_bind_safety(host: str, *, api_key: str) -> None:
    """Refuse to bind non-loopback without an API key.

    Raises ``SystemExit`` if *host* is not a loopback address and
    *api_key* is empty.
    """
    import ipaddress
    import sys

    try:
        is_loop = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loop = host in ("localhost", "")

    if not is_loop and not api_key:
        logger.error(
            "Binding to %s requires OPENJARVIS_API_KEY to be set. "
            "Run: jarvis auth generate-key",
            host,
        )
        sys.exit(1)


def _websocket_key_protocol(api_key: str) -> str:
    """Encode an API key as a browser-safe WebSocket protocol token."""
    try:
        encoded = base64.urlsafe_b64encode(api_key.encode("utf-8")).decode("ascii")
    except UnicodeEncodeError:
        return ""
    return f"{_WS_KEY_PROTOCOL_PREFIX}{encoded.rstrip('=')}"


def _offered_websocket_auth(
    websocket,
) -> tuple[str, str, str | None]:  # noqa: ANN001
    """Return WebSocket credential type, credential, and negotiated protocol.

    Browser clients cannot set arbitrary WebSocket headers, so they may send
    either a master API key or a human session token as a marked subprotocol
    credential.

    Returns:
        A tuple of ``(credential_type, credential, selected_protocol)`` where
        credential_type is ``"api_key"`` or ``"session"``.
    """
    offered = getattr(websocket, "scope", {}).get("subprotocols", [])
    if not isinstance(offered, (list, tuple)) or len(offered) != 2:
        return "", "", None

    if not all(isinstance(protocol, str) for protocol in offered):
        return "", "", None

    stable_protocols = {
        _WS_AUTH_PROTOCOL,
        _WS_SESSION_PROTOCOL,
    }

    selected_protocols = [
        protocol for protocol in offered if protocol in stable_protocols
    ]
    credentials = [
        protocol
        for protocol in offered
        if protocol not in stable_protocols
    ]

    if len(selected_protocols) != 1 or len(credentials) != 1:
        return "", "", None

    stable_protocol = selected_protocols[0]
    credential = credentials[0]

    if stable_protocol == _WS_AUTH_PROTOCOL:
        if not credential.startswith(_WS_KEY_PROTOCOL_PREFIX):
            return "", "", None
        if credential == _WS_KEY_PROTOCOL_PREFIX:
            return "", "", None
        return "api_key", credential, stable_protocol

    if stable_protocol == _WS_SESSION_PROTOCOL:
        if not credential.startswith(_WS_SESSION_PROTOCOL_PREFIX):
            return "", "", None
        if credential == _WS_SESSION_PROTOCOL_PREFIX:
            return "", "", None
        return "session", credential, stable_protocol

    return "", "", None

def authenticate_websocket(
    websocket,  # noqa: ANN001
    expected_key: str,
) -> tuple[bool, str | None, str | None]:
    """Authenticate a WebSocket connection.

    Supports:

    * ``Authorization: Bearer <master API key>`` for programmatic clients.
    * ``openjarvis.auth.v1`` + encoded API key for browser clients.
    * ``X-OpenJarvis-Session`` for programmatic clients using a human session.
    * ``openjarvis.session.v1`` + encoded session token for browser clients.

    Returns:
        ``(authorized, negotiated_subprotocol, user_id)``.

        ``user_id`` is populated only when the connection is authenticated
        using a human session. Master API-key authentication returns ``None``.
    """
    credential_type, credential_protocol, selected_protocol = (
        _offered_websocket_auth(websocket)
    )

    # Programmatic human-session authentication.
    session_header = websocket.headers.get("x-openjarvis-session", "").strip()

    # Programmatic master API-key authentication.
    auth = websocket.headers.get("authorization", "")
    scheme, _, header_token = auth.partition(" ")
    header_valid = (
        bool(expected_key)
        and scheme.lower() == "bearer"
        and _api_keys_match(header_token, expected_key)
    )

    # Browser master API-key authentication.
    expected_key_protocol = _websocket_key_protocol(expected_key)
    protocol_key_valid = (
        credential_type == "api_key"
        and bool(expected_key_protocol)
        and _api_keys_match(credential_protocol, expected_key_protocol)
    )

    if header_valid or protocol_key_valid:
        return True, selected_protocol, None

    # Browser session-token authentication.
    if credential_type == "session":
        expected_prefix = _WS_SESSION_PROTOCOL_PREFIX
        if credential_protocol.startswith(expected_prefix):
            encoded = credential_protocol[len(expected_prefix):]

            try:
                padding = "=" * (-len(encoded) % 4)
                session_token = base64.urlsafe_b64decode(
                    encoded + padding
                ).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                session_token = ""

            if session_token:
                from openjarvis.server.auth_store import AuthStore

                store = getattr(websocket.app.state, "auth_store", None)
                if store is None:
                    store = AuthStore()
                    websocket.app.state.auth_store = store

                user = store.get_user_for_token(session_token)
                if user is not None:
                    user_id = str(user["user_id"])
                    websocket.state.auth_user_id = user_id
                    websocket.state.auth_user = user
                    return True, selected_protocol, user_id

    # Programmatic session-token authentication.
    if session_header:
        from openjarvis.server.auth_store import AuthStore

        store = getattr(websocket.app.state, "auth_store", None)
        if store is None:
            store = AuthStore()
            websocket.app.state.auth_store = store

        user = store.get_user_for_token(session_header)
        if user is not None:
            user_id = str(user["user_id"])
            websocket.state.auth_user_id = user_id
            websocket.state.auth_user = user

            # There is no negotiated protocol when authentication arrived
            # through a normal WebSocket header.
            return True, selected_protocol, user_id

    # Preserve keyless/local behavior.
    if not expected_key:
        return True, selected_protocol, None

    return False, selected_protocol, None

def websocket_authorized(websocket, expected_key: str) -> bool:  # noqa: ANN001
    """Return ``True`` if a WebSocket connection is authenticated.

    ``AuthMiddleware`` is a ``BaseHTTPMiddleware`` and never sees WebSocket
    upgrade requests, so streaming endpoints must check the token themselves
    in the handshake before calling ``websocket.accept()``.

    When *expected_key* is empty, authentication is disabled (the loopback /
    local-only default, matching :class:`AuthMiddleware`) and all connections
    are allowed. See :func:`authenticate_websocket` for the supported
    credential transports. URL query parameters are deliberately not accepted
    because request targets commonly appear in access logs and browser history.
    """
    return authenticate_websocket(websocket, expected_key)[0]
