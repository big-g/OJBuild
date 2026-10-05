"""Legacy migration never invokes servers, leaks credentials or grants approval."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.core.config import JarvisConfig
from openjarvis.mcp.legacy_import import LegacyMCPImporter
from openjarvis.mcp.runtime_manager import RuntimeMCPManager
from openjarvis.server.auth_middleware import AuthMiddleware
from openjarvis.server.auth_store import AuthStore
from openjarvis.server.runtime_mcp_router import create_runtime_mcp_router
from openjarvis.tools.runtime_store import RuntimeToolConflict

SECRET = "LEGACY-TOKEN-DO-NOT-LEAK"


def legacy(**changes):
    return {
        "name": "example",
        "url": "https://example.com/mcp",
        "token": SECRET,
    } | changes


@pytest.fixture
def setup(tmp_path):
    app = FastAPI()
    app.state.config = JarvisConfig()
    app.state.config.tools.mcp.servers = json.dumps([legacy()])
    app.state.auth_store = AuthStore(tmp_path / "auth.db")
    auth = app.state.auth_store
    headers = {}
    for uid in ("admin", "user"):
        auth.create_user(uid, uid, "test-password")
        headers[uid] = {"X-OpenJarvis-Session": auth.create_session(uid)}
    auth.set_admin("admin", True)
    manager = RuntimeMCPManager(tmp_path / "mcp.db")
    app.include_router(create_runtime_mcp_router(manager))
    app.add_middleware(AuthMiddleware, api_key="master")
    return TestClient(app), app, manager, headers


def test_admin_only_review_and_import(setup):
    client, app, manager, headers = setup
    path = "/v1/runtime-mcp/imports/legacy"
    entry = client.get(path, headers=headers["admin"]).json()["entries"][0]
    body = {"index": entry["index"], "review_digest": entry["review_digest"]}
    for h in ({}, headers["user"], {"Authorization": "Bearer master"}):
        for method in ("GET", "POST"):
            response = client.request(
                method, path, headers=h, json=body if method == "POST" else None
            )
            assert response.status_code in {401, 403}
            assert SECRET not in response.text
    assert manager.store.list() == []


def test_review_import_encryption_and_restart(setup, monkeypatch):
    client, app, manager, headers = setup
    monkeypatch.setattr(
        manager, "session", lambda *_: pytest.fail("Import connected to MCP")
    )
    path = "/v1/runtime-mcp/imports/legacy"
    review = client.get(path, headers=headers["admin"])
    assert review.status_code == 200 and SECRET not in review.text
    entry = review.json()["entries"][0]
    assert entry["status"] == "ready" and entry["has_token"]
    assert manager.store.list() == []
    response = client.post(
        path,
        headers=headers["admin"],
        json={"index": 0, "review_digest": entry["review_digest"]},
    )
    assert response.status_code == 201 and SECRET not in response.text
    saved = response.json()
    assert not saved["enabled"] and not saved["approved"] and not saved["discovered"]
    assert not saved["allow_without_confirmation"] and saved["tools"] == []
    assert manager.store.token(manager.store.get(saved["id"])) == SECRET
    assert SECRET.encode() not in manager.store.path.read_bytes()
    audit = manager.store.audit(saved["id"])
    assert audit[0]["event"] == "legacy_imported"
    assert audit[0]["actor"] == "user:admin"
    assert SECRET not in json.dumps(audit)
    assert manager.available() == []
    assert json.loads(app.state.config.tools.mcp.servers)[0]["token"] == SECRET
    restarted = RuntimeMCPManager(manager.store.path)
    assert restarted.available() == []
    assert restarted.store.token(restarted.store.get(saved["id"])) == SECRET
    assert (
        client.get(path, headers=headers["admin"]).json()["entries"][0]["status"]
        == "already_saved"
    )
    assert (
        client.post(
            path,
            headers=headers["admin"],
            json={"index": 0, "review_digest": entry["review_digest"]},
        ).status_code
        == 409
    )
    assert manager.store.get(saved["id"])["revision"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"token": "ROTATED-SECRET"},
        {"url": "https://example.org/mcp"},
        {"name": "changed"},
    ],
)
def test_review_is_bound_to_source_including_token(setup, change):
    client, app, manager, headers = setup
    path = "/v1/runtime-mcp/imports/legacy"
    entry = client.get(path, headers=headers["admin"]).json()["entries"][0]
    app.state.config.tools.mcp.servers = json.dumps([legacy(**change)])
    result = client.post(
        path,
        headers=headers["admin"],
        json={"index": 0, "review_digest": entry["review_digest"]},
    )
    assert result.status_code == 409
    assert SECRET not in result.text and "ROTATED-SECRET" not in result.text
    assert manager.store.list() == []


@pytest.mark.parametrize(
    "item",
    [
        legacy(command="dangerous"),
        legacy(args=["secret"]),
        legacy(include_tools=["safe"]),
        legacy(exclude_tools=["dangerous"]),
        legacy(headers={"X-Token": SECRET}),
        legacy(url="http://example.com/mcp"),
        legacy(url="https://127.0.0.1/mcp"),
        legacy(url="https://example.com/mcp?token=" + SECRET),
        legacy(url="https://example.com/" + SECRET),
        legacy(url="https://example.com/" + "".join(f"%{ord(c):02x}" for c in SECRET)),
        legacy(name=SECRET),
        legacy(token={"password": SECRET}),
        legacy(name=["invalid"]),
        "malformed",
        1,
    ],
)
def test_unsupported_entries_are_blocked_without_echo(setup, item):
    client, app, manager, headers = setup
    app.state.config.tools.mcp.servers = [item]
    path = "/v1/runtime-mcp/imports/legacy"
    response = client.get(path, headers=headers["admin"])
    assert response.status_code == 200, response.text
    assert SECRET not in response.text
    entry = response.json()["entries"][0]
    assert entry["status"] == "blocked" and "url" not in entry and "name" not in entry
    assert (
        client.post(
            path,
            headers=headers["admin"],
            json={"index": 0, "review_digest": entry["review_digest"]},
        ).status_code
        == 400
    )
    assert manager.store.list() == []


def test_duplicate_names_and_concurrent_imports(tmp_path):
    manager = RuntimeMCPManager(tmp_path / "mcp.db")
    importer = LegacyMCPImporter(manager)
    assert all(e["status"] == "blocked" for e in importer.review([legacy(), legacy()]))
    raw = [legacy()]
    entry = importer.review(raw)[0]

    def run(_):
        try:
            return importer.import_one(raw, 0, entry["review_digest"], "user:admin")
        except RuntimeToolConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sum(r is not None for r in results) == 1
    assert len(manager.store.list()) == 1
    assert len(manager.store.audit(manager.store.list()[0]["id"])) == 1


def test_invalid_limits_extra_fields_and_review_after_restart(setup):
    client, app, manager, headers = setup
    path = "/v1/runtime-mcp/imports/legacy"
    h = headers["admin"]
    entry = client.get(path, headers=h).json()["entries"][0]
    body = {"index": 0, "review_digest": entry["review_digest"]}
    assert (
        client.post(path, headers=h, json=body | {"token": SECRET}).status_code == 422
    )
    assert client.post(path, headers=h, json=body | {"index": True}).status_code == 422
    assert client.post(path, headers=h, json=body | {"index": 1}).status_code == 409
    importer = LegacyMCPImporter(manager)
    with pytest.raises(RuntimeToolConflict):
        importer.import_one([legacy()], 0, entry["review_digest"], "user:admin")
    for raw in (
        "invalid " + SECRET,
        json.dumps([legacy()] * 129),
        "x" * (256 * 1024 + 1),
    ):
        app.state.config.tools.mcp.servers = raw
        response = client.get(path, headers=h)
        assert response.status_code == 400 and SECRET not in response.text


def test_external_config_is_read_only_and_changes_invalidate_review(tmp_path):
    manager = RuntimeMCPManager(tmp_path / "mcp.db")
    importer = LegacyMCPImporter(manager)
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps([legacy()]))
    before = path.read_bytes()
    entry = importer.review(str(path))[0]
    importer.import_one(str(path), 0, entry["review_digest"], "user:admin")
    assert path.read_bytes() == before
    manager.store.change(manager.store.list()[0]["id"], 1, "deleted", "user:admin")
    path.write_text(json.dumps([legacy(token="ROTATED-SECRET")]))
    with pytest.raises(RuntimeToolConflict):
        importer.import_one(str(path), 0, entry["review_digest"], "user:admin")
