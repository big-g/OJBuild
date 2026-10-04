"""Vault encryption, fail-closed keys and authenticated source lifecycle."""

import json
import sqlite3
import threading

import httpx
import pytest

from openjarvis.connectors.source_manager import SourceManager
from openjarvis.connectors.source_store import SourceConflict, SourceStore

SECRET = "test-protected-token-987654321"


@pytest.fixture
def manager(tmp_path):
    return SourceManager(
        SourceStore(str(tmp_path / "sources.db"), legacy_path=str(tmp_path / "legacy")),
        knowledge_path=str(tmp_path / "knowledge.db"),
    )


def credential(manager, **kwargs):
    return manager.credentials.create(
        "API token",
        kwargs.pop("kind", "bearer"),
        "https://api.example.com",
        SECRET,
        **kwargs,
    )


def source(manager, identity):
    return manager.create(
        "json_api",
        "Private records",
        {"url": "https://api.example.com/data", "credential_id": identity},
    )


def response(body=b'{"status":"approved"}', url="https://api.example.com/data"):
    return httpx.Response(
        200,
        content=body,
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", url),
    )


def test_encrypted_storage_and_metadata_only(manager):
    record = credential(manager)
    assert SECRET not in json.dumps(record)
    assert record == manager.credentials.list()[0]
    source(manager, record["id"])
    assert SECRET not in json.dumps(manager.list())
    for file in manager.store.path.parent.iterdir():
        if file.is_file():
            assert SECRET.encode() not in file.read_bytes(), file
    assert manager.credentials.path.stat().st_mode & 0o777 == 0o600
    assert manager.credentials.key_path.stat().st_mode & 0o777 == 0o600
    with manager.credentials.bound(
        record["id"], "https://api.example.com/data", ("bearer",)
    ) as row:
        assert manager.credentials.material(row)["headers"] == {
            "Authorization": f"Bearer {SECRET}"
        }


@pytest.mark.parametrize("secret", ["", "line\nbreak", "space token", "é", "x" * 8193])
def test_secret_validation_does_not_echo_input(manager, secret):
    with pytest.raises(ValueError) as error:
        manager.credentials.create("Name", "bearer", "https://api.example.com", secret)
    if secret:
        assert secret not in str(error.value)
    assert manager.credentials.list() == []


@pytest.mark.parametrize(
    "header",
    [
        "Host",
        "Authorization",
        "Cookie",
        "Connection",
        "Proxy-Authorization",
        "X\r\nInjected",
        "Accept",
        "Content-Length",
    ],
)
def test_reserved_and_invalid_headers_rejected(manager, header):
    with pytest.raises(ValueError):
        credential(manager, kind="api_key", header_name=header)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/data",
        "https://other.example.com/data",
        "https://api.example.com:80/data",
    ],
)
def test_source_cannot_use_wrong_origin(manager, url):
    record = credential(manager)
    with pytest.raises(ValueError):
        manager.create(
            "json_api", "Wrong scope", {"url": url, "credential_id": record["id"]}
        )
    assert manager.store.list() == []


def test_missing_key_never_silently_replaces_existing_key(manager):
    record = credential(manager)
    manager.credentials.key_path.unlink()
    with manager.credentials.bound(
        record["id"], "https://api.example.com/data", ("bearer",)
    ) as row:
        with pytest.raises(ValueError, match="key is missing"):
            manager.credentials.material(row)
    assert not manager.credentials.key_path.exists()
    with pytest.raises(ValueError, match="key is missing"):
        credential(manager)


def test_ciphertext_and_binding_tampering_fail_closed(manager):
    record = credential(manager)
    with sqlite3.connect(manager.credentials.path) as conn:
        conn.execute(
            "UPDATE credentials SET origin='https://other.example.com' WHERE id=?",
            (record["id"],),
        )
    with manager.credentials.bound(
        record["id"], "https://other.example.com/data", ("bearer",)
    ) as row:
        with pytest.raises(ValueError, match="could not be unlocked"):
            manager.credentials.material(row)


def test_rotation_revision_and_reference_safe_delete(manager):
    record = credential(manager)
    connection = source(manager, record["id"])
    with pytest.raises(SourceConflict, match="attached"):
        manager.delete_credential(record["id"], 1)
    newer = manager.credentials.rotate(record["id"], 1, "replacement-token-123456")
    assert newer["revision"] == 2
    with pytest.raises(SourceConflict):
        manager.credentials.rotate(record["id"], 1, "stale")
    manager.delete(connection["id"], 1)
    manager.delete_credential(record["id"], 2)
    assert manager.credentials.list() == []


def test_authenticated_probe_and_sync_keep_secrets_out_of_index(manager, monkeypatch):
    record = credential(manager)
    connection = source(manager, record["id"])
    calls = []

    def fetch(url, **kwargs):
        calls.append(kwargs["authentication"]["headers"].copy())
        return response()

    monkeypatch.setattr("openjarvis.connectors.web_sources.fetch_public_source", fetch)
    assert manager.test("json_api", connection["config"])["documents"] == 1
    assert manager.sync(connection["id"]) == 1
    assert calls == [{"Authorization": f"Bearer {SECRET}"}] * 2
    for file in manager.store.path.parent.iterdir():
        if file.is_file():
            assert SECRET.encode() not in file.read_bytes()


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"echo": SECRET}).encode(),
        ("{" + json.dumps(SECRET) + ":1," + json.dumps(SECRET) + ":2}").encode(),
        b'{"echo":"' + SECRET.encode() + b'",broken',
    ],
)
def test_reflected_secret_never_reaches_errors_or_evidence(manager, monkeypatch, body):
    record = credential(manager)
    connection = source(manager, record["id"])
    monkeypatch.setattr(
        "openjarvis.connectors.web_sources.fetch_public_source",
        lambda *a, **kw: response(body),
    )
    with pytest.raises(ValueError) as error:
        manager.sync(connection["id"])
    assert SECRET not in str(error.value)
    assert manager.list()[0]["chunks"] == 0
    assert SECRET not in json.dumps(manager.list())
    with pytest.raises(ValueError) as error:
        manager.test("json_api", connection["config"])
    assert SECRET not in str(error.value)


def test_rotation_and_removal_rejected_during_fetch(manager, monkeypatch):
    record = credential(manager)
    connection = source(manager, record["id"])
    entered, release = threading.Event(), threading.Event()
    errors = []

    def fetch(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return response()

    def sync():
        try:
            manager.sync(connection["id"])
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr("openjarvis.connectors.web_sources.fetch_public_source", fetch)
    worker = threading.Thread(target=sync)
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(SourceConflict, match="in use"):
            manager.credentials.rotate(record["id"], 1, "replacement")
        with pytest.raises(SourceConflict, match="in use"):
            manager.delete_credential(record["id"], 1)
    finally:
        release.set()
        worker.join(5)
    assert not errors


def test_unknown_reference_rejected_and_api_key_binding(manager, monkeypatch):
    with pytest.raises(ValueError):
        source(manager, "00000000-0000-0000-0000-000000000001")
    record = credential(manager, kind="api_key", header_name="X-Api-Key")
    configured = source(manager, record["id"])
    manager.store.set_sharing(
        configured["id"], configured["revision"], "shared", actor="user:admin"
    )

    def fetch(*args, **kwargs):
        assert kwargs["authentication"]["headers"] == {"X-Api-Key": SECRET}
        return response()

    monkeypatch.setattr("openjarvis.connectors.web_sources.fetch_public_source", fetch)
    assert list(manager.collect("json_api"))[0].content == '{"status":"approved"}'


def test_pending_source_binding_prevents_credential_deletion(manager, monkeypatch):
    record = credential(manager)
    entered, release = threading.Event(), threading.Event()
    original = manager.store.create
    errors = []

    def pending(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    def create():
        try:
            source(manager, record["id"])
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(manager.store, "create", pending)
    worker = threading.Thread(target=create)
    worker.start()
    try:
        assert entered.wait(5)
        other = SourceManager(
            SourceStore(
                str(manager.store.path),
                legacy_path=str(manager.store.path.parent / "legacy"),
            ),
            knowledge_path=manager.knowledge_path,
        )
        with pytest.raises(SourceConflict, match="in use"):
            other.delete_credential(record["id"], 1)
    finally:
        release.set()
        worker.join(5)
    assert not errors
    assert manager.store.list()[0]["config"]["credential_id"] == record["id"]
