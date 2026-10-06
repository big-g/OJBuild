"""Saved connections, safe discovery and human-admin access across restarts."""

import sqlite3

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.engine.connection_store import (
    ConnectionConflict,
    ModelConnectionStore,
    definition,
)
from openjarvis.server.auth_store import AuthStore
from openjarvis.server.model_connections_router import create_model_connections_router


@pytest.fixture
def setup(tmp_path):
    app = FastAPI()
    auth = AuthStore(tmp_path / "auth.db")
    auth.create_user("admin", "admin", "test-password", is_admin=True)
    auth.create_user("user", "user", "test-password")
    app.state.auth_store = auth
    store = ModelConnectionStore(tmp_path / "models.db")
    app.include_router(create_model_connections_router(store))
    client = TestClient(app)
    headers = {
        uid: {"X-OpenJarvis-Session": auth.create_session(uid)}
        for uid in ("admin", "user")
    }
    return client, store, headers, auth


def create(setup, **extra):
    client, _, headers, _ = setup
    result = client.post(
        "/v1/model-connections",
        headers=headers["admin"],
        json={"name": "home_gpu", "url": "http://localhost:11434", **extra},
    )
    assert result.status_code == 201, result.text
    return result.json()


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "", None),
        ("POST", "", {"name": "x", "url": "http://localhost:11434"}),
        (
            "PUT",
            "/missing",
            {"name": "x", "url": "http://localhost:11434", "revision": 1},
        ),
        ("DELETE", "/missing?revision=1", None),
        ("GET", "/missing/audit", None),
        ("POST", "/missing/test", {"revision": 1}),
    ],
)
@pytest.mark.parametrize("user,status", [("user", 403), ("key", 401), ("missing", 401)])
def test_every_route_requires_live_human_admin(setup, method, path, body, user, status):
    client, store, headers, _ = setup
    claimed = {
        "Authorization": "Bearer master",
        "X-Is-Admin": "true",
        "X-User-Id": "admin",
    }
    if user == "user":
        claimed.update(headers[user])
    assert (
        client.request(
            method, "/v1/model-connections" + path, headers=claimed, json=body
        ).status_code
        == status
    )
    assert store.list() == []


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:11434",
        "http://8.8.8.8:11434",
        "http://169.254.169.254",
        "http://0.0.0.0:11434",
        "http://[fe80::1]",
        "http://[::]",
        "file:///tmp/x",
        "http://user:SECRET@127.0.0.1",
        "http://localhost:11434/api/tags",
        "http://localhost:11434?token=SECRET",
        "http://localhost:11434#SECRET",
        "http://localhost:0",
        "http://localhost:99999",
        "http://localhost:11434/\n",
        "http://localhost:11434?",
        "http://localhost:11434#",
    ],
)
def test_invalid_endpoints_never_echo_input(setup, url):
    client, store, headers, _ = setup
    result = client.post(
        "/v1/model-connections",
        headers=headers["admin"],
        json={"name": "gpu", "url": url},
    )
    assert result.status_code == 400
    assert "SECRET" not in result.text
    assert not store.list()


@pytest.mark.parametrize(
    "url,normalized",
    [
        ("http://localhost:11434/", "http://127.0.0.1:11434"),
        ("http://192.168.1.20:11434", "http://192.168.1.20:11434"),
        ("https://[fd00::1]:11434", "https://[fd00::1]:11434"),
        ("http://[::1]:11434", "http://[::1]:11434"),
    ],
)
def test_explicit_network_targets(url, normalized):
    assert definition("gpu", url)["url"] == normalized


def test_persistence_identity_stale_edits_and_retained_audit(setup):
    client, store, headers, _ = setup
    a = create(setup)
    b = create(setup, name="other_gpu", url="http://192.168.1.20:11434")
    assert a["id"] != b["id"]
    assert a["discovery_state"] == "untested" and a["catalog"] == []
    store.discovered(a["id"], 1, [{"serving_id": "qwen3.5:9b"}], True, "admin")
    restored = ModelConnectionStore(store.path)
    assert restored.get(a["id"])["catalog"][0]["serving_id"] == "qwen3.5:9b"
    path = "/v1/model-connections/" + a["id"]
    edit = {"name": a["name"], "url": b["url"], "revision": 1}
    assert client.put(path, headers=headers["admin"], json=edit).status_code == 409
    edit["revision"] = 2
    updated = client.put(path, headers=headers["admin"], json=edit).json()
    assert (
        updated["revision"] == 3
        and not updated["catalog"]
        and updated["tested_at"] == 0
    )
    assert (
        client.delete(path + "?revision=2", headers=headers["admin"]).status_code == 409
    )
    assert (
        client.delete(path + "?revision=3", headers=headers["admin"]).status_code == 204
    )
    assert [e["event"] for e in restored.audit(a["id"])] == [
        "created",
        "discovered",
        "edited",
        "removed",
    ]
    assert restored.list()[0]["id"] == b["id"]
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_conflicts_and_no_network_during_configuration(setup, monkeypatch):
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: pytest.fail("Configuration must not connect")
    )
    create(setup)
    client, store, headers, _ = setup
    result = client.post(
        "/v1/model-connections",
        headers=headers["admin"],
        json={"name": "home_gpu", "url": "http://127.0.0.1:1234"},
    )
    assert result.status_code == 409
    assert len(store.list()) == 1
    result = client.post(
        "/v1/model-connections",
        headers=headers["admin"],
        json={"name": "Bad Name", "url": "http://localhost:11434"},
    )
    assert result.status_code == 400 and "Example: home_gpu" in result.text
    result = client.post(
        "/v1/model-connections",
        headers=headers["admin"],
        json={"name": "gpu", "url": "http://localhost", "token": "SECRET"},
    )
    assert result.status_code == 422 and "SECRET" not in result.text


def mock_catalog(monkeypatch, handler):
    real = httpx.Client
    seen = []

    def factory(**kwargs):
        seen.append(kwargs)
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("openjarvis.engine.connection_discovery.httpx.Client", factory)
    return seen


def test_discovery_reads_only_catalog_and_separates_same_model_on_two_servers(
    setup, monkeypatch
):
    a, b = create(setup), create(setup, name="second", url="http://192.168.1.20:11434")
    targets = []

    def handler(request):
        targets.append(str(request.url))
        assert request.method == "GET" and request.url.path == "/api/tags"
        return httpx.Response(
            200, json={"models": [{"name": "qwen3.5:9b", "size": 6600000000}]}
        )

    seen = mock_catalog(monkeypatch, handler)
    client, store, headers, _ = setup
    for connection in (a, b):
        result = client.post(
            f"/v1/model-connections/{connection['id']}/test",
            headers=headers["admin"],
            json={"revision": 1},
        )
        assert result.status_code == 200 and result.json()["ok"]
        row = result.json()["connection"]
        assert row["catalog"] == [
            {
                "serving_id": "qwen3.5:9b",
                "size_bytes": 6600000000,
                "capability_state": "unknown",
            }
        ]
        assert row["revision"] == 2
    assert targets == [
        "http://127.0.0.1:11434/api/tags",
        "http://192.168.1.20:11434/api/tags",
    ]
    assert all(
        not kw["trust_env"] and not kw["follow_redirects"] and kw["timeout"] == 10
        for kw in seen
    )
    assert len(store.list()) == 2


@pytest.mark.parametrize(
    "kind",
    ["redirect", "timeout", "html", "invalid", "duplicate", "large", "many", "secret"],
)
def test_failed_probe_clears_stale_catalog_and_never_echoes_remote_content(
    setup, monkeypatch, kind
):
    connection = create(setup)
    client, store, headers, _ = setup
    store.discovered(connection["id"], 1, [{"serving_id": "old"}], True, "admin")
    requests = []

    def handler(request):
        requests.append(request)
        if kind == "redirect":
            return httpx.Response(
                302, headers={"location": "http://169.254.169.254/SECRET"}
            )
        if kind == "timeout":
            raise httpx.ReadTimeout("SECRET", request=request)
        if kind == "html":
            return httpx.Response(200, text="<script>SECRET</script>")
        if kind == "invalid":
            return httpx.Response(200, json={"models": ["SECRET"]})
        if kind == "duplicate":
            return httpx.Response(200, json={"models": [{"name": "x"}, {"name": "x"}]})
        if kind == "many":
            return httpx.Response(
                200, json={"models": [{"name": str(n)} for n in range(501)]}
            )
        content = b"x" * 1048577 if kind == "large" else b"SECRET"
        return httpx.Response(
            200, content=content, headers={"content-type": "application/json"}
        )

    mock_catalog(monkeypatch, handler)
    result = client.post(
        f"/v1/model-connections/{connection['id']}/test",
        headers=headers["admin"],
        json={"revision": 2},
    )
    assert result.status_code == 200 and not result.json()["ok"]
    assert "SECRET" not in result.text
    assert len(requests) == 1
    row = store.get(connection["id"])
    assert (
        row["discovery_state"] == "error"
        and row["catalog"] == []
        and row["revision"] == 3
    )


def test_probe_cannot_commit_to_edited_connection(setup, monkeypatch):
    a = create(setup)
    client, store, headers, _ = setup

    def discover(_):
        store.update(a["id"], 1, "changed", "http://192.168.1.20:11434", "admin")
        return [{"serving_id": "stale"}]

    monkeypatch.setattr("openjarvis.server.model_connections_router.discover", discover)
    result = client.post(
        f"/v1/model-connections/{a['id']}/test",
        headers=headers["admin"],
        json={"revision": 1},
    )
    assert result.status_code == 409
    assert store.get(a["id"])["catalog"] == []


def test_admin_revocation_blocks_test_commit(setup, monkeypatch):
    a = create(setup)
    client, store, headers, auth = setup

    def discover(_):
        auth.set_admin("admin", False)
        return []

    monkeypatch.setattr("openjarvis.server.model_connections_router.discover", discover)
    result = client.post(
        f"/v1/model-connections/{a['id']}/test",
        headers=headers["admin"],
        json={"revision": 1},
    )
    assert result.status_code in (401, 403)
    assert store.get(a["id"])["revision"] == 1


def test_future_schema_rejected_and_stale_discovery_transaction_rolls_back(tmp_path):
    store = ModelConnectionStore(tmp_path / "models.db")
    row = store.create("gpu", "http://localhost:11434", "admin")
    with pytest.raises(ConnectionConflict):
        store.discovered(row["id"], 99, [], True, "admin")
    assert len(store.audit(row["id"])) == 1
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="Unsupported"):
        ModelConnectionStore(store.path)


def test_configured_database_path_and_connection_limit(tmp_path):
    from openjarvis.core.config import load_config

    path = tmp_path / "config.toml"
    path.write_text('[security]\nmodel_connections_db_path = "/mnt/ai/models.db"\n')
    assert load_config(path).security.model_connections_db_path == "/mnt/ai/models.db"
    store = ModelConnectionStore(tmp_path / "models.db")
    for index in range(64):
        store.create(f"gpu_{index}", "http://localhost:11434", "admin")
    with pytest.raises(ValueError, match="Maximum"):
        store.create("overflow", "http://localhost:11434", "admin")
    assert len(store.list()) == 64


def test_adapter_metadata_and_migration_preserve_saved_connections(setup):
    client, store, headers, _ = setup
    row = create(setup)
    result = client.get("/v1/model-connections/adapters", headers=headers["admin"])
    adapter = result.json()["adapters"][0]
    assert adapter["config_version"] == row["config_version"] == 1
    assert adapter["operations"] == ["catalog_discovery"]
    assert adapter["requires_administrator"]
    assert adapter["required_capabilities"] == []
    # Reconstruct the v1 layout, then exercise the additive migration.
    with sqlite3.connect(store.path) as db:
        db.execute("ALTER TABLE model_connections DROP COLUMN config_version")
        db.execute("PRAGMA user_version=1")
    migrated = ModelConnectionStore(store.path)
    assert migrated.get(row["id"]) == row
    assert migrated.audit(row["id"])[0]["event"] == "created"


def test_unsupported_adapter_version_never_connects(setup, monkeypatch):
    row = create(setup)
    client, store, headers, _ = setup
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE model_connections SET config_version=99 WHERE id=?", (row["id"],)
        )
    monkeypatch.setattr(
        "openjarvis.server.model_connections_router.discover",
        lambda _: pytest.fail("Unknown versions must not connect"),
    )
    result = client.post(
        f"/v1/model-connections/{row['id']}/test",
        headers=headers["admin"],
        json={"revision": 1},
    )
    assert result.status_code == 400 and "Upgrade" in result.text
    assert store.get(row["id"])["revision"] == 1
