"""Shared OAuth 2.0 helpers for all connectors.

Provides:
- ``OAuthProvider`` registry with configs for Google, Strava, Spotify
- Generic ``run_connector_oauth()`` that opens browser + catches callback
- URL builder, token persistence, and token cleanup utilities
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from openjarvis.core import open_browser
from openjarvis.core.config import DEFAULT_CONFIG_DIR

# ---------------------------------------------------------------------------
# Connector credentials directory
# ---------------------------------------------------------------------------

_CONNECTORS_DIR = DEFAULT_CONFIG_DIR / "connectors"

# ---------------------------------------------------------------------------
# OAuth provider registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OAuthProvider:
    """Configuration for an OAuth 2.0 provider."""

    name: str  # "google", "strava", "spotify"
    display_name: str
    auth_endpoint: str
    token_endpoint: str
    scopes: List[str]
    setup_url: str  # URL where user creates OAuth credentials
    setup_hint: str  # One-line instruction for setup
    callback_port: int = 8789
    callback_host: str = "127.0.0.1"
    callback_path: str = "/callback"
    token_auth: str = "body"  # "body" or "basic"
    extra_auth_params: Dict[str, str] = field(default_factory=dict)
    # Connector IDs supported by this provider; consent targets one connector.
    connector_ids: Tuple[str, ...] = ()
    # Filenames in ~/.openjarvis/connectors/ to save tokens to
    credential_files: Tuple[str, ...] = ()
    pkce: bool = False


# Legacy scope list retained for explicit callers. Interactive connector flows
# now use connector_scopes() and never request this combined grant by default.
GOOGLE_ALL_SCOPES: List[str] = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/drive.readonly",
    # calendar (not .readonly) so the proactive agent can accept/decline events.
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/contacts.readonly",
    # gmail.modify (a superset of gmail.readonly) so the proactive agent
    # can trash and label-modify (archive) emails after user approval.
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/tasks.readonly",
]

OAUTH_PROVIDERS: Dict[str, OAuthProvider] = {
    "google": OAuthProvider(
        name="google",
        pkce=True,
        display_name="Google",
        auth_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        scopes=GOOGLE_ALL_SCOPES,
        setup_url="https://console.cloud.google.com/apis/credentials",
        setup_hint=(
            "Create a Web application client for server callbacks, "
            "or a Desktop app client for native loopback flows"
        ),
        extra_auth_params={"access_type": "offline", "prompt": "consent"},
        connector_ids=(
            "gdrive",
            "gcalendar",
            "gcontacts",
            "gmail",
            "google_tasks",
        ),
        credential_files=(
            "google.json",
            "gdrive.json",
            "gcalendar.json",
            "gcontacts.json",
            "gmail.json",
            "google_tasks.json",
        ),
    ),
    "strava": OAuthProvider(
        name="strava",
        display_name="Strava",
        auth_endpoint="https://www.strava.com/oauth/authorize",
        token_endpoint="https://www.strava.com/oauth/token",
        scopes=["activity:read_all"],
        setup_url="https://www.strava.com/settings/api",
        setup_hint="Create an API Application (callback domain: localhost)",
        connector_ids=("strava",),
        credential_files=("strava.json",),
    ),
    "spotify": OAuthProvider(
        name="spotify",
        pkce=True,
        display_name="Spotify",
        auth_endpoint="https://accounts.spotify.com/authorize",
        token_endpoint="https://accounts.spotify.com/api/token",
        scopes=["user-read-recently-played"],
        setup_url="https://developer.spotify.com/dashboard",
        setup_hint=("Create an app, add redirect URI: http://127.0.0.1:8888/callback"),
        callback_port=8888,
        token_auth="basic",
        connector_ids=("spotify",),
        credential_files=("spotify.json",),
    ),
}


def get_provider_for_connector(connector_id: str) -> Optional[OAuthProvider]:
    """Return the OAuthProvider that covers *connector_id*, or ``None``."""
    for provider in OAUTH_PROVIDERS.values():
        if connector_id in provider.connector_ids:
            return provider
    return None


_GOOGLE_READ_SCOPES = {
    "gdrive": "https://www.googleapis.com/auth/drive.readonly",
    "gcalendar": "https://www.googleapis.com/auth/calendar.readonly",
    "gcontacts": "https://www.googleapis.com/auth/contacts.readonly",
    "gmail": "https://www.googleapis.com/auth/gmail.readonly",
    "google_tasks": "https://www.googleapis.com/auth/tasks.readonly",
}


def connector_scopes(provider, connector_id):
    if connector_id not in provider.connector_ids:
        raise ValueError("Connector does not belong to this OAuth provider")
    return (
        [_GOOGLE_READ_SCOPES[connector_id]]
        if provider.name == "google"
        else list(provider.scopes)
    )


def connector_credential_file(provider, connector_id):
    if connector_id not in provider.connector_ids:
        raise ValueError("Connector does not belong to this OAuth provider")
    filename = f"{connector_id}.json"
    if filename not in provider.credential_files:
        raise ValueError("No credential destination for this connector")
    return filename


def pkce_pair():
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------


def get_client_credentials(
    provider: OAuthProvider,
) -> Optional[Tuple[str, str]]:
    """Load stored client_id and client_secret for *provider*.

    Checks credential files in ``~/.openjarvis/connectors/`` and falls
    back to environment variables ``OPENJARVIS_{NAME}_CLIENT_ID`` and
    ``OPENJARVIS_{NAME}_CLIENT_SECRET``.
    """
    # Check credential files
    for filename in provider.credential_files:
        path = _CONNECTORS_DIR / filename
        tokens = load_tokens(str(path))
        if tokens and tokens.get("client_id") and tokens.get("client_secret"):
            return tokens["client_id"], tokens["client_secret"]

    # Check environment variables
    prefix = f"OPENJARVIS_{provider.name.upper()}"
    env_id = os.environ.get(f"{prefix}_CLIENT_ID", "")
    env_secret = os.environ.get(f"{prefix}_CLIENT_SECRET", "")
    if env_id and env_secret:
        return env_id, env_secret

    return None


def save_client_credentials(
    provider: OAuthProvider,
    client_id: str,
    client_secret: str,
) -> None:
    """Persist client credentials so the user never has to enter them again."""
    for filename in provider.credential_files:
        path = _CONNECTORS_DIR / filename
        existing = load_tokens(str(path)) or {}
        existing["client_id"] = client_id
        existing["client_secret"] = client_secret
        save_tokens(str(path), existing)


# ---------------------------------------------------------------------------
# Legacy shared Google credentials fallback; new consent writes one connector.
# ---------------------------------------------------------------------------

_SHARED_GOOGLE_CREDENTIALS_PATH: str = str(_CONNECTORS_DIR / "google.json")

_GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_DEFAULT_REDIRECT_URI = "http://localhost:8789/callback"
_DEFAULT_SCOPES: List[str] = ["openid", "email", "profile"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_google_auth_url(
    client_id: str,
    redirect_uri: str = _DEFAULT_REDIRECT_URI,
    scopes: Optional[List[str]] = None,
    *,
    state: str = "",
    code_challenge: str = "",
) -> str:
    """Build a Google OAuth2 consent URL.

    Parameters
    ----------
    client_id:
        The OAuth 2.0 client ID from the Google Cloud Console.
    redirect_uri:
        Where Google should redirect after consent. Defaults to the local
        callback server at ``http://localhost:8789/callback``.
    scopes:
        List of OAuth scopes to request.  Defaults to
        ``["openid", "email", "profile"]``.

    Returns
    -------
    str
        Full consent URL including query string.
    """
    if scopes is None:
        scopes = _DEFAULT_SCOPES

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
    }
    if state:
        params["state"] = state
    if code_challenge:
        params.update(code_challenge=code_challenge, code_challenge_method="S256")
    return f"{_GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}"


def resolve_google_credentials(connector_path: str) -> str:
    """Return the best available Google credentials file path.

    Checks the connector-specific file first, then falls back to the
    shared ``google.json``.  Returns *connector_path* if neither exists
    (so ``is_connected()`` correctly returns ``False``).
    """
    if Path(connector_path).exists():
        return connector_path
    if Path(_SHARED_GOOGLE_CREDENTIALS_PATH).exists():
        return _SHARED_GOOGLE_CREDENTIALS_PATH
    return connector_path


def load_tokens(path: str) -> Optional[Dict[str, Any]]:
    """Load an encrypted bundle, migrating legacy JSON before returning it."""
    from openjarvis.connectors.token_vault import TokenVault

    p = Path(path)
    if not p.exists() and not p.is_symlink():
        return None
    return TokenVault(p).load()


def save_tokens(path: str, tokens: Dict[str, Any]) -> None:
    """Encrypt tokens and client secrets; persist only a reference at *path*."""
    from openjarvis.connectors.token_vault import TokenVault

    TokenVault(path).save(tokens)


def require_access_token(tokens: Any) -> str:
    """Return a non-empty OAuth access token or reject the response.

    Client registration credentials and error-shaped token responses are not
    authenticated sessions.  Centralizing this check keeps every OAuth entry
    point from persisting a false-success credential file.
    """
    if not isinstance(tokens, dict):
        raise RuntimeError("OAuth token response was not a JSON object")
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise RuntimeError("OAuth token response did not include an access_token")
    if len(access_token) > 8192 or any(
        ord(c) < 33 or ord(c) > 126 for c in access_token
    ):
        raise RuntimeError("OAuth token response included an invalid access_token")
    if str(tokens.get("token_type", "Bearer")).lower() != "bearer":
        raise RuntimeError("OAuth token response used an unsupported token_type")
    return access_token.strip()


def delete_tokens(path: str) -> None:
    """Remove the connector reference and its encrypted credential bundle."""
    from openjarvis.connectors.token_vault import TokenVault

    p = Path(path)
    if p.parent.exists():
        TokenVault(p).delete()


def refresh_google_token(path: str) -> Optional[str]:
    """Compatibility wrapper around the validated, redacted refresh implementation."""
    from openjarvis.connectors.google_auth import GoogleAuthError, refresh_access_token

    try:
        return refresh_access_token(path)
    except GoogleAuthError:
        return None


def exchange_google_token(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str = _DEFAULT_REDIRECT_URI,
    *,
    code_verifier: str = "",
) -> Dict[str, Any]:
    """Compatibility exchange through the bounded, redacted provider helper."""
    return _exchange_token(
        OAUTH_PROVIDERS["google"],
        code,
        client_id,
        client_secret,
        redirect_uri,
        code_verifier=code_verifier,
    )


def run_oauth_flow(
    client_id: str,
    client_secret: str,
    scopes: List[str],
    credentials_path: str,
    redirect_uri: str = _DEFAULT_REDIRECT_URI,
) -> Dict[str, Any]:
    """Google native flow with S256 PKCE and an exact, bounded loopback callback."""
    from urllib.parse import urlparse

    from openjarvis.connectors.oauth_state import validate_callback_uri

    validate_callback_uri(redirect_uri)
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Native OAuth requires an HTTP loopback callback")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(32)
    auth_url = build_google_auth_url(
        client_id, redirect_uri, scopes, state=state, code_challenge=challenge
    )
    code = _wait_for_callback_code(
        host=parsed.hostname,
        port=parsed.port or 8789,
        path=parsed.path,
        expected_state=state,
        open_url=auth_url,
    )
    tokens = _exchange_token(
        OAUTH_PROVIDERS["google"],
        code,
        client_id,
        client_secret,
        redirect_uri,
        code_verifier=verifier,
    )
    save_tokens(
        credentials_path,
        {
            "access_token": require_access_token(tokens),
            "refresh_token": tokens.get("refresh_token", ""),
            "token_type": tokens.get("token_type", "Bearer"),
            "expires_in": tokens.get("expires_in", 3600),
            "client_id": client_id,
            "client_secret": client_secret,
            "requested_scopes": scopes,
        },
    )
    return tokens


def _wait_for_callback_code(
    *,
    expected_state: str,
    open_url: str,
    host: str = "127.0.0.1",
    port: int = 8789,
    path: str = "/callback",
    timeout: int = 120,
) -> str:
    """Listen before opening consent; reject invalid paths/state until deadline."""
    import time
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    if (
        host not in {"127.0.0.1", "localhost"}
        or not expected_state
        or not path.startswith("/")
    ):
        raise ValueError("OAuth listener requires a loopback host, path and state")
    deadline = time.monotonic() + timeout
    code, denied = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            try:
                params = parse_qs(
                    parsed.query, keep_blank_values=True, max_num_fields=20
                )
            except ValueError:
                params = {}
            valid = (
                len(self.path) <= 8192
                and parsed.path == path
                and len(params.get("state", [])) == 1
                and secrets.compare_digest(
                    params["state"][0].encode(), expected_state.encode()
                )
                and (
                    (len(params.get("code", [])) == 1 and not params.get("error"))
                    or (len(params.get("error", [])) == 1 and not params.get("code"))
                )
            )
            if not valid:
                self.send_response(400)
                self.end_headers()
                return
            if params.get("error"):
                denied.append(True)
            elif params.get("code", [""])[0]:
                code.append(params["code"][0])
            else:
                self.send_response(400)
                self.end_headers()
                return
            self.send_response(400 if denied else 200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
            )
            self.end_headers()
            self.wfile.write(
                b"<html><body>You can close this tab and return to "
                b"OpenJarvis.</body></html>"
            )

        def log_message(self, *_args):
            pass

    class CallbackServer(HTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(max(0.01, min(5, deadline - time.monotonic())))
            return connection, address

        def handle_error(self, *_args):
            pass  # Never print a callback URL/code in request failure logs.

    server = CallbackServer((host, port), Handler)
    server.timeout = 0.5
    try:
        open_browser(open_url)
        while not code and not denied and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if denied:
        raise RuntimeError("OAuth authorization denied")
    if not code:
        raise RuntimeError("OAuth callback timed out")
    return code[0]


def _exchange_token(
    provider: OAuthProvider,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    *,
    code_verifier: str = "",
) -> Dict[str, Any]:
    """Exchange an authorization *code* for tokens using *provider* config."""
    import httpx

    data: Dict[str, str] = {
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    headers: Dict[str, str] = {}

    if code_verifier:
        data["code_verifier"] = code_verifier
    if provider.name == "spotify" and code_verifier:
        data["client_id"] = client_id
    elif provider.token_auth == "basic":
        creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {creds}"
    else:
        data["client_id"] = client_id
        data["client_secret"] = client_secret

    try:
        resp = httpx.post(
            provider.token_endpoint,
            data=data,
            headers=headers,
            timeout=30.0,
            follow_redirects=False,
            trust_env=False,
        )
        resp.raise_for_status()
        if len(resp.content) > 65536:
            raise ValueError("Token response exceeds limit")
        tokens = resp.json()
    except Exception:
        raise RuntimeError(
            "OAuth token exchange failed; start authorization again"
        ) from None
    require_access_token(tokens)
    return tokens


def run_connector_oauth(
    connector_id: str,
    client_id: str = "",
    client_secret: str = "",
) -> Dict[str, Any]:
    """Run a complete OAuth flow for *connector_id*.

    1. Look up the ``OAuthProvider``
    2. Resolve client credentials (arg → stored → env)
    3. Build auth URL and open the user's browser
    4. Start localhost callback server and wait for the code
    5. Exchange the code for tokens
    6. Save tokens only to the selected connector's credential file

    Returns the raw token response dict.
    """

    provider = get_provider_for_connector(connector_id)
    if provider is None:
        raise ValueError(f"No OAuth provider configured for '{connector_id}'")

    # Resolve credentials
    if not (client_id and client_secret):
        creds = get_client_credentials(provider)
        if creds:
            client_id, client_secret = creds
    if not (client_id and client_secret):
        raise RuntimeError(
            f"No client credentials for {provider.display_name}. "
            f"Set them up at: {provider.setup_url}"
        )

    redirect_uri = (
        f"http://{provider.callback_host}:{provider.callback_port}"
        f"{provider.callback_path}"
    )

    state = secrets.token_urlsafe(32)
    verifier, challenge = pkce_pair() if provider.pkce else ("", "")
    scopes = connector_scopes(provider, connector_id)
    # Build auth URL
    params: Dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        **provider.extra_auth_params,
        "state": state,
    }
    if challenge:
        params.update(code_challenge=challenge, code_challenge_method="S256")
    auth_url = f"{provider.auth_endpoint}?{urlencode(params)}"

    # Listen before opening consent; bind callback to this request.
    code = _wait_for_callback_code(
        host=provider.callback_host,
        port=provider.callback_port,
        path=provider.callback_path,
        expected_state=state,
        open_url=auth_url,
    )

    # Exchange code for tokens
    tokens = _exchange_token(
        provider, code, client_id, client_secret, redirect_uri, code_verifier=verifier
    )
    access_token = require_access_token(tokens)

    # Build payload with client credentials included (needed for refresh)
    payload = {
        "access_token": access_token,
        "refresh_token": tokens.get("refresh_token", ""),
        "token_type": tokens.get("token_type", "Bearer"),
        "expires_in": tokens.get("expires_in", 3600),
        "client_id": client_id,
        "client_secret": client_secret,
    }

    payload["requested_scopes"] = scopes
    save_tokens(
        str(_CONNECTORS_DIR / connector_credential_file(provider, connector_id)),
        payload,
    )

    return tokens
