"""Hermetic encrypted bundle migration and lifecycle regression tests."""

import concurrent.futures
import json
import os
import sqlite3

import httpx
import pytest
from cryptography.fernet import Fernet

from openjarvis.connectors.oauth import delete_tokens, load_tokens, save_tokens
from openjarvis.connectors.source_credentials import CredentialStore
from openjarvis.connectors.source_store import SourceConflict
from openjarvis.connectors.token_vault import TokenVault, migrate_connector_tokens


@pytest.fixture(autouse=True)
def no_provider_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Vault tests must not contact a provider")

    monkeypatch.setattr(httpx.Client, "send", forbidden)


@pytest.fixture
def path(tmp_path):
    directory = tmp_path / "connectors"
    directory.mkdir()
    return directory / "gdrive.json"


def secrets():
    return {
        "access_token": "access-secret-example",
        "refresh_token": "refresh-secret-example",
        "client_secret": "client-secret-example",
        "client_id": "registered-client",
        "expires_at": 1234567,
    }


def row(vault):
    with vault.store._connection() as conn:
        return dict(vault._row(conn))


def test_new_bundle_encrypts_all_values_and_keeps_public_list_private(path):
    save_tokens(str(path), secrets())
    vault = TokenVault(path)
    assert load_tokens(str(path)) == secrets()
    marker = json.loads(path.read_text())
    assert marker == {"openjarvis_token_vault": {"version": 1, "id": vault.identity}}
    for value in secrets().values():
        assert str(value).encode() not in path.read_bytes()
        assert str(value).encode() not in vault.store.path.read_bytes()
    assert vault.store.list() == []
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(vault.store.path).st_mode & 0o777 == 0o600
    assert os.stat(vault.store.key_path).st_mode & 0o777 == 0o600


def test_legacy_migration_is_idempotent_and_survives_reopening(path):
    path.write_text(json.dumps(secrets()))
    assert load_tokens(str(path)) == secrets()
    vault = TokenVault(path)
    original = row(vault)
    assert load_tokens(str(path)) == secrets()
    assert row(TokenVault(path)) == original
    assert migrate_connector_tokens(path.parent) == 0


def test_migration_failure_preserves_plaintext_for_retry(path, monkeypatch):
    path.write_text(json.dumps(secrets()))
    original = path.read_bytes()
    from openjarvis.connectors import token_vault

    write = token_vault.secure_write_json

    def fail(*args):
        raise OSError("simulated atomic file replacement failure")

    monkeypatch.setattr(token_vault, "secure_write_json", fail)
    with pytest.raises(OSError):
        load_tokens(str(path))
    assert path.read_bytes() == original
    monkeypatch.setattr(token_vault, "secure_write_json", write)
    assert load_tokens(str(path)) == secrets()
    assert b"access-secret-example" not in path.read_bytes()


@pytest.mark.parametrize("action", ["missing", "wrong", "invalid"])
def test_key_failure_never_overwrites_existing_ciphertext(path, action):
    save_tokens(str(path), secrets())
    vault = TokenVault(path)
    original = row(vault)
    if action == "missing":
        vault.store.key_path.unlink()
    else:
        vault.store.key_path.write_bytes(
            Fernet.generate_key() if action == "wrong" else b"invalid-key"
        )
    for operation in (
        lambda: load_tokens(str(path)),
        lambda: save_tokens(str(path), {"access_token": "replacement"}),
    ):
        with pytest.raises(ValueError) as error:
            operation()
        assert "access-secret-example" not in str(error.value)
    assert row(vault) == original
    if action == "missing":
        assert not vault.store.key_path.exists()


def test_reference_and_ciphertext_cannot_be_swapped_between_connectors(path):
    other = path.with_name("gmail.json")
    save_tokens(str(path), secrets())
    save_tokens(str(other), {"access_token": "mail-secret"})
    marker = path.read_bytes()
    path.write_bytes(other.read_bytes())
    with pytest.raises(ValueError, match="reference"):
        load_tokens(str(path))
    path.write_bytes(marker)
    vault = TokenVault(path)
    with vault.store._connection() as conn:
        conn.execute(
            "UPDATE connector_tokens SET sealed=? WHERE id=?",
            (row(TokenVault(other))["sealed"], vault.identity),
        )
    with pytest.raises(ValueError, match="decrypted"):
        load_tokens(str(path))


def test_tampered_ciphertext_is_rejected(path):
    save_tokens(str(path), secrets())
    vault = TokenVault(path)
    with vault.store._connection() as conn:
        conn.execute("UPDATE connector_tokens SET sealed=?", (b"corrupt",))
    with pytest.raises(ValueError, match="decrypted"):
        load_tokens(str(path))


def test_refresh_revision_prevents_stale_overwrite_and_resurrection(path):
    save_tokens(str(path), secrets())
    old = load_tokens(str(path))
    current = load_tokens(str(path))
    current["access_token"] = "refreshed-secret"
    save_tokens(str(path), current)
    with pytest.raises(SourceConflict):
        save_tokens(str(path), old)
    current = load_tokens(str(path))
    delete_tokens(str(path))
    with pytest.raises(SourceConflict):
        save_tokens(str(path), current)
    assert load_tokens(str(path)) is None
    with TokenVault(path).store._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM connector_tokens").fetchone()[0] == 0
    delete_tokens(str(path))
    # Explicit new consent can still establish a fresh connection.
    save_tokens(str(path), secrets())
    assert load_tokens(str(path)) == secrets()


def test_parallel_legacy_readers_share_one_migration(path):
    path.write_text(json.dumps(secrets()))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: load_tokens(str(path)), range(12)))
    assert all(result == secrets() for result in results)
    assert row(TokenVault(path))["revision"] == 1


@pytest.mark.parametrize("target", ["file", "parent", "database", "key", "locks"])
def test_symlink_aliases_are_rejected(path, tmp_path, target):
    if target == "file":
        original = tmp_path / "original.json"
        original.write_text(json.dumps(secrets()))
        path.symlink_to(original)
    elif target == "parent":
        alias = tmp_path / "alias"
        alias.symlink_to(path.parent, target_is_directory=True)
        path = alias / path.name
    else:
        names = {
            "database": "source_credentials.db",
            "key": "source_credentials.key",
            "locks": "source-credential-locks",
        }
        (tmp_path / names[target]).symlink_to(tmp_path / "other")
    with pytest.raises(ValueError, match="symbolic"):
        TokenVault(path)


def test_bulk_migration_only_touches_known_credentials(path):
    path.write_text(json.dumps(secrets()))
    local = path.with_name("local_files.json")
    local.write_text('{"path": "/documents"}')
    assert migrate_connector_tokens(path.parent) == 1
    assert migrate_connector_tokens(path.parent) == 0
    assert local.read_text() == '{"path": "/documents"}'


@pytest.mark.parametrize("raw", ["[]", "not-json", "null"])
def test_invalid_legacy_objects_are_not_migrated(path, raw):
    path.write_text(raw)
    assert load_tokens(str(path)) is None
    assert path.read_text() == raw


def test_bundle_size_limits_preserve_existing_values(path):
    save_tokens(str(path), secrets())
    with pytest.raises(ValueError, match="limit"):
        save_tokens(str(path), {"token": "x" * 65536})
    assert load_tokens(str(path)) == secrets()


def test_existing_scalar_vault_remains_compatible(path):
    store = CredentialStore(path.parent.parent / "source_credentials.db")
    credential = store.create(
        "Example", "bearer", "https://example.com", "bearer-secret"
    )
    with sqlite3.connect(store.path) as conn:
        conn.execute("PRAGMA user_version=1")
    save_tokens(str(path), secrets())
    reopened = CredentialStore(store.path)
    assert reopened.list() == [credential]
    with reopened.bound(credential["id"], "https://example.com", ("bearer",)) as bound:
        assert reopened.material(bound)["secret"] == "bearer-secret"


def test_google_refresh_persists_encrypted_and_rejects_disconnect_race(
    path, monkeypatch
):
    from openjarvis.connectors.google_auth import refresh_access_token

    save_tokens(str(path), secrets())

    def refresh(*args, **kwargs):
        return httpx.Response(200, json={"access_token": "fresh-secret"})

    monkeypatch.setattr(httpx, "post", refresh)
    assert refresh_access_token(str(path)) == "fresh-secret"
    assert b"fresh-secret" not in path.read_bytes()
    assert load_tokens(str(path))["refresh_token"] == secrets()["refresh_token"]

    def disconnected_refresh(*args, **kwargs):
        delete_tokens(str(path))
        return refresh()

    monkeypatch.setattr(httpx, "post", disconnected_refresh)
    with pytest.raises(SourceConflict):
        refresh_access_token(str(path))
    assert not path.exists()


def test_missing_database_reference_does_not_generate_replacement_key(path):
    save_tokens(str(path), secrets())
    vault = TokenVault(path)
    vault.store.path.unlink()
    vault.store.key_path.unlink()
    for operation in (
        lambda: load_tokens(str(path)),
        lambda: save_tokens(str(path), secrets()),
    ):
        with pytest.raises(ValueError, match="reference does not exist"):
            operation()
    assert not vault.store.key_path.exists()


def test_full_backup_restored_under_new_home_preserves_binding(path, tmp_path):
    import shutil

    save_tokens(str(path), secrets())
    restored = tmp_path / "restored"
    restored.mkdir()
    (restored / "connectors").mkdir()
    for file in (
        path,
        path.parent.parent / "source_credentials.db",
        path.parent.parent / "source_credentials.key",
    ):
        destination = restored / ("connectors" if file == path else "") / file.name
        shutil.copyfile(file, destination)
    assert load_tokens(str(restored / "connectors" / path.name)) == secrets()


def test_bulk_migration_reports_invalid_files_without_overwriting(path):
    path.write_text("invalid-json")
    with pytest.raises(ValueError, match="valid JSON object"):
        migrate_connector_tokens(path.parent)
    assert path.read_text() == "invalid-json"
