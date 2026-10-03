"""Local IMAP migration preserves passwords, endpoints and independent identities."""

import json

import httpx
import pytest

from openjarvis.connectors.instance_sources import token_path
from openjarvis.connectors.oauth import load_tokens, save_tokens
from openjarvis.connectors.source_audit import list_events
from openjarvis.connectors.source_imports import SourceImports
from openjarvis.connectors.source_manager import SourceManager
from openjarvis.connectors.source_store import SourceConflict, SourceStore

PASSWORD = " protected mailbox password "
USERNAME = "private-user@example.com"


@pytest.fixture
def manager(tmp_path, monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("Imports must not contact mail servers")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    return SourceManager(
        SourceStore(
            str(tmp_path / "sources.db"), legacy_path=str(tmp_path / "missing")
        ),
        knowledge_path=str(tmp_path / "knowledge.db"),
    )


def seed(manager, identity="imap", **settings):
    path = manager.store.path.parent / "connectors" / f"{identity}.json"
    values = {"email": USERNAME, "password": PASSWORD, **settings}
    save_tokens(str(path), values)
    return path, values


def preview(manager, identity="imap"):
    return SourceImports(manager).preview(identity, "Work mailbox", actor="user:owner")


def apply(manager, plan, identity="imap", actor="user:owner"):
    return SourceImports(manager).apply(identity, plan["plan_token"], actor=actor)


@pytest.mark.parametrize("identity", ["imap", "gmail_imap"])
@pytest.mark.parametrize("plaintext", [False, True])
def test_password_import_is_local_encrypted_secret_free_and_independent(
    manager, identity, plaintext
):
    path, values = seed(
        manager,
        identity,
        imap_host="mail.example.com",
        imap_port="1143",
        imap_security="START-TLS",
        token="ignored-secret",
        mailbox="ignored-folder",
    )
    if plaintext:
        path.write_text(json.dumps(values))
    original = path.read_bytes()
    plan = preview(manager, identity)
    assert path.read_bytes() == original
    assert manager.store.list() == []
    assert PASSWORD not in json.dumps(plan) and USERNAME not in json.dumps(plan)
    assert "ignored-secret" not in json.dumps(plan)
    assert plan["connection_auth"] == "password"
    assert not plan["oauth_grant_preserved"] and not plan["refresh_available"]
    assert plan["credential_origin"] == "mail.example.com:1143 (STARTTLS)"
    assert plan["config"] == {
        "host": "mail.example.com",
        "port": 1143,
        "security": "starttls",
        "mailbox": "INBOX",
        "max_messages": 1000,
        "timeout_seconds": 120,
    }
    source = apply(manager, plan, identity)
    assert source == apply(manager, plan, identity)
    assert source["adapter_id"] == "imap_account"
    assert not source["legacy_document_ids"]
    assert manager.list()[0]["chunks"] == 0
    assert load_tokens(str(token_path(path.parent.parent, source["id"]))) == {
        "username": USERNAME,
        "password": PASSWORD,
    }
    assert load_tokens(str(path)) == values
    if not plaintext:
        assert path.read_bytes() == original
    assert PASSWORD.encode() not in path.read_bytes()
    assert PASSWORD.encode() not in manager.credentials.path.read_bytes()
    assert PASSWORD.encode() not in manager.store.path.read_bytes()
    assert PASSWORD not in json.dumps(list_events(manager.store))
    assert USERNAME not in json.dumps(list_events(manager.store))


@pytest.mark.parametrize(
    "identity,email,host",
    [
        ("imap", "user@outlook.com", "outlook.office365.com"),
        ("imap", "user@example.com", "imap.example.com"),
        ("gmail_imap", "user@example.com", "imap.gmail.com"),
    ],
)
def test_legacy_endpoint_defaults_are_local_and_recipe_specific(
    manager, identity, email, host
):
    seed(manager, identity, email=email)
    assert preview(manager, identity)["config"]["host"] == host


@pytest.mark.parametrize(
    "settings",
    [
        {"email": ""},
        {"email": "user name"},
        {"password": ""},
        {"password": "bad\npassword"},
        {"password": "é"},
        {"password": "a" * 4097},
        {"imap_host": "https://mail.example.com"},
        {"imap_security": "plain"},
        {"imap_security": 123},
        {"imap_port": True},
        {"imap_port": 143.9},
        {"imap_port": "1.5"},
        {"imap_port": 65536},
        {"password": "mail", "imap_host": "mail.example.com"},
    ],
)
def test_invalid_or_reflected_credentials_never_become_preview_settings(
    manager, settings
):
    seed(manager, **settings)
    item = next(
        row for row in SourceImports(manager).list() if row["import_id"] == "imap"
    )
    assert item["state"] == "unavailable"
    with pytest.raises(ValueError, match="unavailable"):
        preview(manager)
    assert manager.store.list() == []


@pytest.mark.parametrize(
    "field,value", [("password", "new-password"), ("imap_host", "other.example.com")]
)
def test_changed_password_or_endpoint_rejects_preview(manager, field, value):
    path, values = seed(manager, imap_host="mail.example.com")
    plan = preview(manager)
    save_tokens(str(path), {**values, field: value})
    with pytest.raises(SourceConflict, match="changed"):
        apply(manager, plan)
    assert manager.store.list() == []


def test_password_plans_bind_to_caller_and_recipe(manager):
    seed(manager)
    seed(manager, "gmail_imap")
    plan = preview(manager)
    for identity, actor in [("imap", "user:other"), ("gmail_imap", "user:owner")]:
        with pytest.raises(ValueError, match="invalid or expired"):
            apply(manager, plan, identity, actor)


def test_password_import_recovers_without_replacing_changed_reserved_bundle(
    manager, monkeypatch
):
    seed(manager)
    plan = preview(manager)
    original = manager.store._insert_source

    def fail(*args, **kwargs):
        raise OSError("simulated source commit failure")

    monkeypatch.setattr(manager.store, "_insert_source", fail)
    with pytest.raises(ValueError, match="failed"):
        apply(manager, plan)
    assert manager.store.list() == []
    mapping = SourceImports(manager)._mapping("imap")
    destination = token_path(manager.store.path.parent, mapping["source_id"])
    save_tokens(
        str(destination), {"username": USERNAME, "password": "rotated-password"}
    )
    monkeypatch.setattr(manager.store, "_insert_source", original)
    with pytest.raises(SourceConflict, match="changed"):
        apply(manager, plan)
    assert manager.store.list() == []
    assert load_tokens(str(destination))["password"] == "rotated-password"


def test_password_import_recovers_reserved_identity_and_keeps_tombstone(
    manager, monkeypatch
):
    seed(manager)
    plan = preview(manager)
    original = manager.store._insert_source
    monkeypatch.setattr(
        manager.store,
        "_insert_source",
        lambda *a, **kw: (_ for _ in ()).throw(OSError()),
    )
    with pytest.raises(ValueError):
        apply(manager, plan)
    mapping = SourceImports(manager)._mapping("imap")
    monkeypatch.setattr(manager.store, "_insert_source", original)
    source = apply(manager, plan)
    assert source["id"] == mapping["source_id"]
    manager.delete(source["id"], source["revision"])
    assert load_tokens(str(token_path(manager.store.path.parent, source["id"]))) is None
    with pytest.raises(SourceConflict, match="removed"):
        apply(manager, plan)
