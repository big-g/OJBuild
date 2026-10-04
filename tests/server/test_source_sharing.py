"""Personal ownership, reviewed universal access and per-account consumption."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.connectors.source_access import execution_access
from openjarvis.connectors.source_manager import SourceManager
from openjarvis.connectors.source_store import SourceStore
from openjarvis.core.correlation import ExecutionIdentity, execution_scope
from openjarvis.server.auth_middleware import AuthMiddleware
from openjarvis.server.auth_store import AuthStore
from openjarvis.server.sources_router import create_sources_router


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = SourceStore(
        str(tmp_path / "sources.db"), legacy_path=str(tmp_path / "none")
    )
    manager = SourceManager(store, knowledge_path=str(tmp_path / "knowledge.db"))
    auth = AuthStore(tmp_path / "auth.db")
    for uid in ("alice", "bob", "admin"):
        auth.create_user(uid, uid, "test-password")
    auth.set_admin("admin", True)
    app = FastAPI()
    app.state.auth_store = auth
    app.add_middleware(AuthMiddleware, api_key="master")
    app.include_router(create_sources_router(manager))
    client = TestClient(app, base_url="https://testserver")
    headers = {
        uid: {"X-OpenJarvis-Session": auth.create_session(uid)}
        for uid in ("alice", "bob", "admin")
    }
    root = tmp_path / "docs"
    root.mkdir()
    (root / "weather.txt").write_text("Sunny weather universal forecast")
    result = client.post(
        "/v1/sources",
        headers=headers["alice"],
        json={
            "adapter_id": "local_files",
            "name": "Weather",
            "config": {"path": str(root)},
        },
    )
    assert result.status_code == 201, result.text
    monkeypatch.setattr("openjarvis.connectors.source_store.SourceStore", lambda: store)
    return client, headers, manager, result.json()


def test_private_sharing_and_admin_review(setup):
    client, headers, manager, source = setup
    path = f"/v1/sources/{source['id']}"
    assert source["owner_id"] == "alice"
    assert client.get("/v1/sources", headers=headers["bob"]).json()["sources"] == []
    for method, suffix, body in [
        ("get", "/connection", None),
        ("post", "/sync", None),
        ("get", "/audit", None),
        ("put", "/connection/token", {"revision": 1, "token": "secret"}),
        ("post", "/oauth/start", {}),
    ]:
        assert getattr(client, method)(
            path + suffix, headers=headers["bob"], **({"json": body} if body else {})
        ).status_code in {404, 405}
    assert (
        client.put(
            path + "/sharing",
            headers=headers["alice"],
            json={"revision": 1, "sharing": "shared"},
        ).status_code
        == 403
    )
    response = client.put(
        path + "/sharing",
        headers=headers["alice"],
        json={"revision": 1, "sharing": "pending"},
    )
    assert response.status_code == 200
    assert client.get("/v1/sources", headers=headers["bob"]).json()["sources"] == []
    assert (
        client.put(
            path + "/sharing",
            headers=headers["admin"],
            json={"revision": 2, "sharing": "shared"},
        ).status_code
        == 200
    )
    visible = client.get("/v1/sources", headers=headers["bob"]).json()["sources"][0]
    assert visible["sharing"] == "shared" and visible["can_manage"] is False
    assert visible["config"] == {}
    assert client.post(path + "/sync", headers=headers["bob"]).status_code == 403
    assert client.post(path + "/sync", headers=headers["alice"]).status_code == 403
    assert (
        client.put(
            path,
            headers=headers["admin"],
            json={
                "revision": 3,
                "name": "Changed",
                "enabled": True,
                "config": source["config"],
            },
        ).status_code
        == 200
    )
    assert manager.store.get(source["id"])["sharing"] == "pending"
    assert client.get("/v1/sources", headers=headers["bob"]).json()["sources"] == []


def test_preferences_and_retrieval_do_not_cross_accounts(setup):
    from openjarvis.connectors.store import KnowledgeStore
    from openjarvis.tools.knowledge_sql import KnowledgeSQLTool

    client, headers, manager, source = setup
    manager.sync(source["id"])
    with KnowledgeStore(manager.knowledge_path) as knowledge:
        for uid, expected in [("alice", 1), ("bob", 0)]:
            with execution_scope(ExecutionIdentity(user_id=uid)):
                assert len(knowledge.retrieve("weather")) == expected
                from openjarvis.tools.storage._stubs import RetrievalResult
                from openjarvis.tools.storage.context import trusted_results

                assert (
                    len(
                        trusted_results(
                            [
                                RetrievalResult(
                                    content="private",
                                    score=1,
                                    source="local_files",
                                    metadata={"source_instance_id": source["id"]},
                                )
                            ]
                        )
                    )
                    == expected
                )
                result = KnowledgeSQLTool(knowledge).execute(
                    query="SELECT content FROM knowledge_chunks"
                )
                assert result.success
                assert ("Sunny" in result.content) == bool(expected)
        path = f"/v1/sources/{source['id']}"
        client.put(
            path + "/sharing",
            headers=headers["admin"],
            json={"revision": 1, "sharing": "shared"},
        )
        with execution_scope(ExecutionIdentity(user_id="bob")):
            assert len(knowledge.retrieve("weather")) == 1
            assert execution_access(manager.store).consumes(
                manager.store.get(source["id"])
            )
        assert (
            client.put(
                path + "/preference", headers=headers["bob"], json={"enabled": False}
            ).status_code
            == 200
        )
        with execution_scope(ExecutionIdentity(user_id="bob")):
            assert knowledge.retrieve("weather") == []
        with execution_scope(ExecutionIdentity(user_id="alice")):
            assert len(knowledge.retrieve("weather")) == 1
        client.put(
            path + "/sharing",
            headers=headers["admin"],
            json={"revision": 2, "sharing": "personal"},
        )
        with execution_scope(ExecutionIdentity(user_id="bob")):
            assert knowledge.retrieve("weather") == []


def test_credential_references_cannot_be_stolen(setup):
    client, headers, manager, source = setup
    response = client.post(
        "/v1/sources/credentials",
        headers=headers["alice"],
        json={
            "name": "Private",
            "kind": "bearer",
            "origin": "https://api.example.com",
            "secret": "private-token",
        },
    )
    assert response.status_code == 201
    cid = response.json()["id"]
    assert (
        client.get("/v1/sources/credentials", headers=headers["bob"]).json()[
            "credentials"
        ]
        == []
    )
    assert (
        client.delete(
            f"/v1/sources/credentials/{cid}?revision=1", headers=headers["bob"]
        ).status_code
        == 404
    )
    payload = {
        "adapter_id": "json_api",
        "name": "Steal",
        "config": {"url": "https://api.example.com/data", "credential_id": cid},
    }
    assert (
        client.post("/v1/sources", headers=headers["bob"], json=payload).status_code
        == 404
    )


def test_hybrid_and_live_collect_obey_same_selection(setup):
    from openjarvis.connectors.hybrid_search import HybridSearch
    from openjarvis.connectors.store import KnowledgeStore

    client, headers, manager, source = setup
    manager.sync(source["id"])
    with KnowledgeStore(manager.knowledge_path) as knowledge:
        for uid, expected in [("alice", 1), ("bob", 0)]:
            with execution_scope(ExecutionIdentity(user_id=uid)):
                assert len(HybridSearch(knowledge).search("weather")) == expected
                assert len(list(manager.collect("local_files"))) == expected


def test_revoked_role_is_effective_without_relogin(setup):
    client, headers, manager, source = setup
    assert client.get("/v1/sources", headers=headers["admin"]).json()["can_approve"]
    client.app.state.auth_store.set_admin("admin", False)
    assert not client.get("/v1/sources", headers=headers["admin"]).json()["can_approve"]
    assert (
        client.put(
            f"/v1/sources/{source['id']}/sharing",
            headers=headers["admin"],
            json={"revision": 1, "sharing": "shared"},
        ).status_code
        == 404
    )
    assert (
        client.get("/v1/sources/imports", headers=headers["alice"]).status_code == 403
    )


def test_old_source_schema_recovers_only_proven_creators(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE sources (id TEXT PRIMARY KEY,adapter_id TEXT NOT "
        "NULL,name TEXT NOT NULL,config TEXT NOT NULL,config_version "
        "INTEGER NOT NULL,revision INTEGER NOT NULL,enabled INTEGER NOT "
        "NULL,legacy_document_ids INTEGER NOT NULL,created_at TEXT NOT "
        "NULL,updated_at TEXT NOT NULL,state TEXT NOT NULL DEFAULT "
        "'idle',error TEXT); PRAGMA user_version=4;"
    )
    from openjarvis.connectors.source_audit import SCHEMA, append_event

    conn.executescript(SCHEMA)
    for identity, actor in [("known", "user:alice"), ("legacy", "system")]:
        conn.execute(
            "INSERT INTO sources (id,adapter_id,name,config,config_version,rev"
            "ision,enabled,legacy_document_ids,created_at,updated_at) VALUES "
            "(?,'weather','Old','{}',1,1,1,0,'then','then')",
            (identity,),
        )
        append_event(
            conn,
            {
                "id": identity,
                "adapter_id": "weather",
                "revision": 1,
                "config_version": 1,
            },
            "created",
            actor=actor,
        )
    conn.commit()
    conn.close()
    store = SourceStore(str(path), legacy_path=str(tmp_path / "none"))
    assert store.get("known")["owner_id"] == "alice"
    assert store.get("legacy")["owner_id"] == ""
    assert all(record["sharing"] == "personal" for record in store.list())
    assert (
        SourceStore(str(path), legacy_path=str(tmp_path / "none")).get("known")[
            "revision"
        ]
        == 1
    )


def test_credential_revision_changes_cannot_keep_universal_approval(setup):
    from openjarvis.connectors.source_access import SourceAccess

    client, headers, manager, source = setup
    credential = manager.credentials.create(
        "Key", "bearer", "https://api.example.com", "first-key", owner_id="alice"
    )
    record = manager.create(
        "json_api",
        "API",
        {"url": "https://api.example.com/data", "credential_id": credential["id"]},
        actor="user:alice",
    )
    approved = manager.store.set_sharing(
        record["id"], record["revision"], "shared", actor="user:admin"
    )
    assert SourceAccess(manager.store, "bob").visible(approved)
    # Covers mutation through another process and the approve/rotate race.
    manager.credentials.rotate(credential["id"], 1, "second-key")
    assert not SourceAccess(manager.store, "bob").visible(
        manager.store.get(record["id"])
    )
    assert not SourceAccess(manager.store, "bob").consumes(
        manager.store.get(record["id"])
    )


def test_legacy_credential_access_requires_a_single_proven_owner(setup):
    from openjarvis.connectors.source_access import SourceAccess

    client, headers, manager, source = setup
    credential = manager.credentials.create(
        "Old", "bearer", "https://api.example.com", "old-key"
    )
    assert not SourceAccess(manager.store, "alice").credential_allowed(
        manager.credentials, credential["id"]
    )
    config = {"url": "https://api.example.com/data", "credential_id": credential["id"]}
    manager.create("json_api", "Alice", config, actor="user:alice")
    assert SourceAccess(manager.store, "alice").credential_allowed(
        manager.credentials, credential["id"]
    )
    assert not SourceAccess(manager.store, "bob").credential_allowed(
        manager.credentials, credential["id"]
    )
    manager.create("json_api", "Bob", config, actor="user:bob")
    assert not SourceAccess(manager.store, "alice").credential_allowed(
        manager.credentials, credential["id"]
    )


def test_access_checks_preserve_safe_input_errors(setup):
    client, headers, manager, source = setup
    assert (
        client.post(
            "/v1/sources", headers=headers["alice"], content="not JSON"
        ).status_code
        == 422
    )
    response = client.post(
        "/v1/sources",
        headers=headers["alice"],
        json={
            "adapter_id": "json_api",
            "name": "Invalid",
            "config": {"url": "https://api.example.com", "credential_id": ["bad"]},
        },
    )
    assert response.status_code == 404


def test_oauth_handoff_cannot_outlive_source_administrator_permission(setup):
    from openjarvis.connectors.source_adapters import get_adapter

    client, headers, manager, source = setup
    record = manager.create(
        "strava_account",
        "Alice activities",
        get_adapter("strava_account").validate_config({}),
        actor="user:alice",
    )
    path = f"/v1/sources/{record['id']}"
    assert (
        client.put(
            path + "/connection/client",
            headers=headers["admin"],
            json={"revision": 1, "client_id": "client", "client_secret": "secret"},
        ).status_code
        == 200
    )
    begin = client.post(path + "/oauth/start", headers=headers["admin"])
    assert begin.status_code == 200, begin.text
    client.app.state.auth_store.set_admin("admin", False)
    handoff = client.get(begin.json()["launch_path"], follow_redirects=False)
    assert handoff.status_code == 403


def test_legacy_connectors_need_admin_even_without_a_master_key(setup, monkeypatch):
    client, headers, manager, source = setup
    monkeypatch.delenv("OPENJARVIS_API_KEY", raising=False)
    app = FastAPI()
    app.state.auth_store = client.app.state.auth_store
    app.add_middleware(AuthMiddleware, api_key="")

    @app.get("/v1/connectors")
    def legacy():
        return {"ok": True}

    public = TestClient(app)
    assert public.get("/v1/connectors").status_code == 401
    assert public.get("/v1/connectors", headers=headers["bob"]).status_code == 403
    assert public.get("/v1/connectors", headers=headers["admin"]).status_code == 200


def test_legacy_provider_index_is_not_automatically_shared(setup):
    from openjarvis.connectors.store import KnowledgeStore

    client, headers, manager, source = setup
    with KnowledgeStore(manager.knowledge_path) as knowledge:
        knowledge.store(
            "Legacy private email weather", source="gmail", doc_id="old-email"
        )
        with execution_scope(ExecutionIdentity(user_id="bob")):
            assert knowledge.retrieve("weather") == []
