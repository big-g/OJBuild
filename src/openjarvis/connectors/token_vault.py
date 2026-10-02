"""Encrypted connector bundles with atomic, secret-free legacy file references.

No provider traffic is performed here. Reading a legacy object migrates it before
returning its values. Missing keys and invalid references fail closed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import InvalidToken

from openjarvis.connectors.source_credentials import CredentialStore
from openjarvis.connectors.source_store import SourceConflict
from openjarvis.security.file_utils import secure_write_json

_REFERENCE = "openjarvis_token_vault"
_LIMIT = 65536


class _Bundle(dict):
    """Internal revision carried through refresh read/modify/write operations."""

    def __init__(self, values, identity, revision):
        super().__init__(values)
        self.identity = identity
        self.revision = revision


class TokenVault:
    def __init__(self, path: str | Path):
        self.path = Path(os.path.abspath(path))
        # Reject aliases, including symlinked ancestors, before opening storage.
        if any(part.is_symlink() for part in (self.path, *self.path.parents)):
            raise ValueError("Credential paths must not contain symbolic links")
        directory = self.path.parent
        root = directory.parent if directory.name == "connectors" else directory
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in (
            "source_credentials.db",
            "source_credentials.key",
            "source-credential-locks",
        ):
            if (root / name).is_symlink():
                raise ValueError("Credential paths must not contain symbolic links")
        self.binding = str(self.path.relative_to(root))
        self.identity = str(uuid.uuid5(uuid.NAMESPACE_URL, self.binding))
        self.store = CredentialStore(root / "source_credentials.db")

    def _read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as file:
            raw = file.read(_LIMIT + 1)
        if len(raw) > _LIMIT:
            raise ValueError("Credential bundle exceeds the storage limit")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            return None
        return value if isinstance(value, dict) else None

    def _reference(self, value):
        if _REFERENCE not in value:
            return False
        if value != {_REFERENCE: {"version": 1, "id": self.identity}}:
            raise ValueError("Invalid connector credential reference")
        return True

    def _row(self, conn):
        return conn.execute(
            "SELECT sealed,revision FROM connector_tokens WHERE id=?",
            (self.identity,),
        ).fetchone()

    def _material(self, row, cipher):
        if row is None:
            raise ValueError("Connector credential reference does not exist")
        try:
            payload = json.loads(cipher.decrypt(row["sealed"]))
            if payload["binding"] != self.binding or not isinstance(
                payload["tokens"], dict
            ):
                raise ValueError("Invalid binding")
            return _Bundle(payload["tokens"], self.identity, row["revision"])
        except (InvalidToken, ValueError, KeyError, TypeError):
            raise ValueError("Connector credentials could not be decrypted") from None

    def _write(self, conn, tokens, cipher, row):
        if not isinstance(tokens, dict) or _REFERENCE in tokens:
            raise ValueError("Credential bundle must be a JSON object")
        if isinstance(tokens, _Bundle) and (
            tokens.identity != self.identity
            or row is None
            or tokens.revision != row["revision"]
        ):
            raise SourceConflict("Connector credentials changed; retry the operation")
        payload = json.dumps({"binding": self.binding, "tokens": tokens}).encode()
        if len(payload) > _LIMIT:
            raise ValueError("Credential bundle exceeds the storage limit")
        revision = row["revision"] + 1 if row else 1
        conn.execute(
            "INSERT INTO connector_tokens VALUES (?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET sealed=excluded.sealed, "
            "revision=excluded.revision",
            (self.identity, cipher.encrypt(payload), revision),
        )
        return _Bundle(tokens, self.identity, revision)

    def _marker(self):
        secure_write_json(self.path, {_REFERENCE: {"version": 1, "id": self.identity}})

    @contextmanager
    def inspect(self):
        """Internal snapshot held against refresh/disconnect; do not migrate JSON.

        Callers must never expose yielded values in public records or responses.
        """
        with self.store._lock(self.identity, blocking=True):
            value = self._read()
            if value is not None and self._reference(value):
                with self.store._connection() as conn:
                    row = self._row(conn)
                if row is None:
                    raise ValueError("Connector credential reference does not exist")
                value = self._material(row, self.store._cipher())
            try:
                yield value
            finally:
                if value is not None:
                    value.clear()

    def load(self):
        with self.store._lock(self.identity, exclusive=True, blocking=True):
            value = self._read()
            if value is None:
                return None
            referenced = self._reference(value)
            with self.store._connection() as conn:
                row = self._row(conn)
                if referenced and row is None:
                    raise ValueError("Connector credential reference does not exist")
                cipher = self.store._cipher()
                if referenced:
                    return self._material(row, cipher)
                if row is not None:
                    self._material(row, cipher)
                result = self._write(conn, value, cipher, row)
            # Commit encrypted data before replacing plaintext. A failed atomic
            # replacement retains the original file; retry is safe and idempotent.
            self._marker()
            return result

    def save(self, tokens):
        with self.store._lock(self.identity, exclusive=True, blocking=True):
            previous = self._read()
            referenced = previous is not None and self._reference(previous)
            with self.store._connection() as conn:
                row = self._row(conn)
                if referenced and row is None:
                    raise ValueError("Connector credential reference does not exist")
                cipher = self.store._cipher()
                # Never overwrite inaccessible ciphertext with a new login.
                if row is not None:
                    self._material(row, cipher)
                self._write(conn, tokens, cipher, row)
            if not referenced:
                self._marker()

    @contextmanager
    def imported_bundle(self, tokens):
        """Recover a reserved import without replacing independently changed data.

        The caller holds this binding's exclusive lock through source commit.
        Encrypted data commits first; a failed source transaction can reuse the
        reserved identity. Existing ciphertext must decrypt and match exactly.
        """
        if not isinstance(tokens, dict) or _REFERENCE in tokens:
            raise ValueError("Invalid imported credential bundle")
        try:
            with self.store._lock(self.identity, exclusive=True, blocking=True):
                previous = self._read()
                if previous is None and self.path.exists():
                    raise SourceConflict("Reserved import credential changed")
                referenced = previous is not None and self._reference(previous)
                if previous is not None and not referenced:
                    raise SourceConflict("Reserved import credential changed")
                with self.store._connection() as conn:
                    row = self._row(conn)
                    if referenced and row is None:
                        raise ValueError(
                            "Connector credential reference does not exist"
                        )
                    cipher = self.store._cipher()
                    if row is not None:
                        material = self._material(row, cipher)
                        try:

                            def digest(value):
                                return hashlib.sha256(
                                    json.dumps(
                                        value, sort_keys=True, separators=(",", ":")
                                    ).encode()
                                ).digest()

                            if not hmac.compare_digest(
                                digest(material), digest(tokens)
                            ):
                                raise SourceConflict(
                                    "Reserved import credential changed"
                                )
                        finally:
                            material.clear()
                    else:
                        self._write(conn, dict(tokens), cipher, row)
                if not referenced:
                    self._marker()
                yield
        finally:
            tokens.clear()

    def delete(self):
        with self.store._lock(self.identity, exclusive=True, blocking=True):
            value = self._read()
            if value is not None:
                self._reference(value)
            # Unlink first: an interrupted delete cannot expose a dangling marker
            # as a connected account. A repeated delete also removes orphan rows.
            self.path.unlink(missing_ok=True)
            with self.store._connection() as conn:
                conn.execute(
                    "DELETE FROM connector_tokens WHERE id=?", (self.identity,)
                )


def migrate_connector_tokens(directory: str | Path) -> int:
    """Migrate only known credential files; never ordinary connector configuration.

    Intended for a stopped server's one-time upgrade. Normal connector reads also
    migrate on demand. Returns the number of plaintext files replaced.
    """
    names = (
        "google",
        "gdrive",
        "gcalendar",
        "gcontacts",
        "gmail",
        "google_tasks",
        "spotify",
        "strava",
        "notion",
        "dropbox",
        "slack",
        "granola",
        "gmail_imap",
        "oura",
        "github",
        "github_notifications",
        "weather",
    )
    migrated = 0
    for name in names:
        path = Path(directory) / f"{name}.json"
        if path.exists() or path.is_symlink():
            vault = TokenVault(path)
            with vault.store._lock(vault.identity, exclusive=True, blocking=True):
                value = vault._read()
                if value is None:
                    raise ValueError(
                        "Legacy credential file is not a valid JSON object"
                    )
                legacy = not vault._reference(value)
            vault.load()
            migrated += int(legacy)
    return migrated
