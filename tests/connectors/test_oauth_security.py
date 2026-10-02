"""Mock-only OAuth protocol/state security; never access real service accounts."""

import concurrent.futures
from pathlib import Path

import httpx
import pytest

from openjarvis.connectors.oauth import (
    OAUTH_PROVIDERS,
    _exchange_token,
    connector_scopes,
    load_tokens,
    pkce_pair,
    require_access_token,
    save_tokens,
)
from openjarvis.connectors.oauth_state import (
    InvalidOAuthAttempt,
    OAuthStateStore,
    validate_callback_uri,
)


def payload():
    verifier, challenge = pkce_pair()
    return dict(
        provider="google",
        client_id="test-id",
        redirect_uri="https://jarvis.example.test/v1/connectors/gdrive/oauth/callback",
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
        code_verifier=verifier,
        code_challenge=challenge,
        actor="owner",
    )


def issued(tmp_path):
    store = OAuthStateStore(tmp_path)
    data = payload()
    attempt = store.create("gdrive", data, now=1000)
    state, browser, opened = store.launch("gdrive", attempt["ticket"], now=1001)
    return store, data, attempt, state, browser


def test_handoff_persists_encrypted_and_is_single_use(tmp_path):
    store, data, attempt, state, browser = issued(tmp_path)
    raw = store.path.read_bytes()
    assert data["code_verifier"].encode() not in raw
    assert attempt["ticket"].encode() not in raw
    assert state.encode() not in raw and browser.encode() not in raw
    assert store.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(InvalidOAuthAttempt):
        store.launch("gdrive", attempt["ticket"], now=1002)
    reopened = OAuthStateStore(tmp_path)
    accepted = reopened.consume(
        "gdrive", state, browser, data["redirect_uri"], now=1003
    )
    assert accepted["code_verifier"] == data["code_verifier"]
    assert reopened.status("gdrive", attempt["attempt_id"], "owner", now=1003) == {
        "status": "consumed"
    }
    reopened.finish(accepted, success=True)
    assert store.status("gdrive", attempt["attempt_id"], "owner", now=1003) == {
        "status": "completed"
    }
    with pytest.raises(InvalidOAuthAttempt):
        store.consume("gdrive", state, browser, data["redirect_uri"], now=1004)


@pytest.mark.parametrize(
    "change", ["connector", "state", "browser", "redirect", "expired"]
)
def test_callback_binding_rejects_swapping_without_consuming_valid_attempt(
    tmp_path, change
):
    store, data, attempt, state, browser = issued(tmp_path)
    connector, uri, now = "gdrive", data["redirect_uri"], 1003
    if change == "connector":
        connector = "gmail"
    if change == "state":
        state = "different"
    if change == "browser":
        browser = "different"
    if change == "redirect":
        uri = "https://other.example.test/callback"
    if change == "expired":
        now = 1601
    with pytest.raises(InvalidOAuthAttempt):
        store.consume(connector, state, browser, uri, now=now)
    assert (
        store.status("gdrive", attempt["attempt_id"], "owner", now=1003)["status"]
        == "issued"
    )
    with pytest.raises(InvalidOAuthAttempt):
        store.status("gdrive", attempt["attempt_id"], "different-owner", now=1003)


def test_pending_expiry_capacity_and_disconnect(tmp_path):
    store = OAuthStateStore(tmp_path)
    data = payload()
    first = store.create("gdrive", data, now=1000)
    with pytest.raises(InvalidOAuthAttempt):
        store.launch("gdrive", first["ticket"], now=1060)
    for _ in range(100):
        store.create("gdrive", data, now=1100)
    with pytest.raises(ValueError, match="Too many"):
        store.create("gdrive", data, now=1100)
    store.cancel("gdrive")
    assert store.create("gdrive", data, now=1101)


def test_two_workers_cannot_consume_same_callback(tmp_path):
    store, data, attempt, state, browser = issued(tmp_path)

    def consume():
        try:
            OAuthStateStore(tmp_path).consume(
                "gdrive", state, browser, data["redirect_uri"], now=1003
            )
            return True
        except InvalidOAuthAttempt:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: consume(), range(2))) == [False, True]


def test_context_tampering_fails_closed(tmp_path):
    store, data, attempt, state, browser = issued(tmp_path)
    with store.connection() as conn:
        conn.execute("UPDATE oauth_attempts SET connector_id='gmail'")
    with pytest.raises(InvalidOAuthAttempt):
        store.consume("gmail", state, browser, data["redirect_uri"], now=1003)


@pytest.mark.parametrize(
    "uri",
    [
        "http://jarvis.lan/callback",
        "http://192.168.1.2/callback",
        "https://user:password@example.test/callback",
        "https://example.test/callback?token=x",
    ],
)
def test_callback_transport_rejects_insecure_lan_and_embedded_credentials(uri):
    with pytest.raises(ValueError):
        validate_callback_uri(uri)


def test_google_consent_only_requests_one_read_service():
    provider = OAUTH_PROVIDERS["google"]
    for connector in provider.connector_ids:
        scopes = connector_scopes(provider, connector)
        assert len(scopes) == 1 and scopes[0].endswith(".readonly")
    with pytest.raises(ValueError):
        connector_scopes(provider, "spotify")


def test_exchange_uses_pkce_and_never_forwards_secrets_on_redirect(monkeypatch):
    captured = []

    def post(url, **kwargs):
        captured.append(kwargs)
        return httpx.Response(
            302,
            headers={"location": "https://evil.example.test"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(RuntimeError, match="OAuth token exchange failed"):
        _exchange_token(
            OAUTH_PROVIDERS["spotify"],
            "code",
            "id",
            "protected-secret",
            "https://jarvis.example.test/callback",
            code_verifier="verifier",
        )
    call = captured[0]
    assert call["follow_redirects"] is False and call["trust_env"] is False
    assert (
        call["data"]["code_verifier"] == "verifier"
        and call["data"]["client_id"] == "id"
    )
    assert (
        "client_secret" not in call["data"] and "Authorization" not in call["headers"]
    )


def test_refresh_error_redacts_remote_body_and_keeps_previous_tokens(
    tmp_path, monkeypatch
):
    from openjarvis.connectors.google_auth import GoogleAuthError, refresh_access_token

    path = str(tmp_path / "tokens.json")
    tokens = {
        "access_token": "old",
        "refresh_token": "refresh",
        "client_id": "id",
        "client_secret": "secret",
    }
    save_tokens(path, tokens)

    def post(url, **kwargs):
        return httpx.Response(400, text="reflected-secret<script>bad</script>")

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(GoogleAuthError) as error:
        refresh_access_token(path)
    assert "reflected-secret" not in str(error.value) and "script" not in str(
        error.value
    )
    assert load_tokens(str(Path(path))) == tokens


@pytest.mark.parametrize(
    "tokens",
    [
        {"access_token": "bad\r\ntoken"},
        {"access_token": "a" * 8193},
        {"access_token": "token", "token_type": "DPoP"},
        [],
        {"refresh_token": "only"},
    ],
)
def test_invalid_token_payloads_are_not_authenticated(tokens):
    with pytest.raises(RuntimeError):
        require_access_token(tokens)


def test_native_listener_binds_before_browser_and_requires_exact_state_path(
    monkeypatch,
):
    import http.server
    import io

    import openjarvis.connectors.oauth as oauth

    requests = [
        "/wrong?state=expected&code=injected",
        "/callback?state=wrong&code=injected",
        "/callback?state=expected&state=expected&code=injected",
        "/callback?state=expected&code=valid",
    ]
    opened, closed = [], []

    class FakeServer:
        bound = False

        def __init__(self, address, handler):
            FakeServer.bound = True
            self.handler = handler

        def handle_request(self):
            request = self.handler.__new__(self.handler)
            request.path = requests.pop(0)
            request.wfile = io.BytesIO()
            request.do_GET()

        def server_close(self):
            closed.append(True)

    def browser(url):
        assert FakeServer.bound
        opened.append(url)

    monkeypatch.setattr(http.server, "HTTPServer", FakeServer)
    monkeypatch.setattr(
        http.server.BaseHTTPRequestHandler, "send_response", lambda *args: None
    )
    monkeypatch.setattr(
        http.server.BaseHTTPRequestHandler, "send_header", lambda *args: None
    )
    monkeypatch.setattr(
        http.server.BaseHTTPRequestHandler, "end_headers", lambda *args: None
    )
    monkeypatch.setattr(oauth, "open_browser", browser)
    assert (
        oauth._wait_for_callback_code(
            expected_state="expected", open_url="https://consent.example.test"
        )
        == "valid"
    )
    assert opened == ["https://consent.example.test"] and closed == [True]


def test_native_connector_flow_requests_only_selected_service_and_pkce(
    tmp_path, monkeypatch
):
    from urllib.parse import parse_qs, urlparse

    import openjarvis.connectors.oauth as oauth

    monkeypatch.setattr(oauth, "_CONNECTORS_DIR", tmp_path)
    captured = {}

    def callback(**kwargs):
        query = parse_qs(urlparse(kwargs["open_url"]).query)
        assert query["state"] == [kwargs["expected_state"]]
        assert query["scope"] == ["https://www.googleapis.com/auth/tasks.readonly"]
        assert query["code_challenge_method"] == ["S256"]
        captured.update(query)
        return "code"

    def exchange(provider, code, client, secret, redirect, **kwargs):
        import base64
        import hashlib

        expected = (
            base64.urlsafe_b64encode(
                hashlib.sha256(kwargs["code_verifier"].encode()).digest()
            )
            .decode()
            .rstrip("=")
        )
        assert captured["code_challenge"] == [expected]
        return {"access_token": "test-access"}

    monkeypatch.setattr(oauth, "_wait_for_callback_code", callback)
    monkeypatch.setattr(oauth, "_exchange_token", exchange)
    oauth.run_connector_oauth("google_tasks", "id", "secret")
    assert (tmp_path / "google_tasks.json").exists()
    assert (
        not (tmp_path / "gmail.json").exists()
        and not (tmp_path / "google.json").exists()
    )


@pytest.mark.parametrize(
    "target",
    [
        "/v1/connectors/gdrive/oauth/callback?code=secret&state=state",
        "/v1/connectors/spotify/oauth/launch?ticket=secret",
        "/v1/connectors/gdrive/oauth/%63allback?code=secret",
    ],
)
def test_standard_access_logs_redact_sensitive_oauth_queries(target):
    import logging

    from openjarvis.server.oauth_logging import OAuthAccessFilter

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("client", "GET", target, "1.1", 200),
        None,
    )
    assert OAuthAccessFilter().filter(record)
    assert "secret" not in record.getMessage() and "[redacted]" in record.getMessage()


def test_access_filter_preserves_ordinary_queries_and_installs_once():
    import logging

    from openjarvis.server.oauth_logging import (
        OAuthAccessFilter,
        install_oauth_access_filter,
    )

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("client", "GET", "/v1/models?format=json", "1.1", 200),
        None,
    )
    OAuthAccessFilter().filter(record)
    assert "format=json" in record.getMessage()
    install_oauth_access_filter()
    install_oauth_access_filter()
    assert (
        len(
            [
                item
                for item in logging.getLogger("uvicorn.access").filters
                if isinstance(item, OAuthAccessFilter)
            ]
        )
        == 1
    )
