"""Server-owned encrypted credentials. Public records contain metadata only."""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from openjarvis.connectors.source_store import SourceConflict
from openjarvis.security.public_http import normalize_source_url

_COLUMNS = "id,name,kind,origin,header_name,revision,created_at,updated_at"
_RESERVED = {
    "authorization",
    "host",
    "cookie",
    "accept",
    "accept-encoding",
    "connection",
    "content-length",
    "transfer-encoding",
    "user-agent",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "upgrade",
}


def credential_origin(url: str) -> str:
    normalized = normalize_source_url(url)
    parsed = urlparse(normalized)
    if parsed.scheme != "https":
        raise ValueError("Authenticated connections require HTTPS")
    return f"https://{parsed.netloc}".removesuffix(":443")


def _secret(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 8192
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
    ):
        raise ValueError(
            "Credential must contain 1–8192 printable ASCII characters "
            "without whitespace"
        )
    return value


class CredentialStore:
    def __init__(self, path: Path):
        self.path = path
        self.key_path = path.with_suffix(".key")
        self.lock_dir = path.parent / "source-credential-locks"
        self.lock_dir.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with self._lock("schema", exclusive=True, blocking=True):
            with self._connection() as conn:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version > 2:
                    raise ValueError("Unsupported credential database version")
                conn.execute("""CREATE TABLE IF NOT EXISTS credentials (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                    origin TEXT NOT NULL, header_name TEXT NOT NULL,
                    sealed BLOB NOT NULL,
                    revision INTEGER NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )""")
                conn.execute("""CREATE TABLE IF NOT EXISTS connector_tokens (
                    id TEXT PRIMARY KEY, sealed BLOB NOT NULL,
                    revision INTEGER NOT NULL
                )""")
                conn.execute("PRAGMA user_version=2")

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def _lock(self, identity: str, *, exclusive=False, blocking=False):
        if identity != "schema":
            identity = str(uuid.UUID(identity))
        fd = os.open(
            self.lock_dir / identity, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            try:
                fcntl.flock(fd, operation | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                raise SourceConflict(
                    "Credential is in use; retry after the operation finishes"
                ) from None
            yield
        finally:
            os.close(fd)

    def _cipher(self):
        from cryptography.fernet import Fernet

        with self._lock("schema", exclusive=True, blocking=True):
            if not self.key_path.exists():
                with self._connection() as conn:
                    if (
                        conn.execute("SELECT 1 FROM credentials LIMIT 1").fetchone()
                        or conn.execute(
                            "SELECT 1 FROM connector_tokens LIMIT 1"
                        ).fetchone()
                    ):
                        raise ValueError(
                            "Credential key is missing; restore the original key backup"
                        )
                fd = os.open(
                    self.key_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
                with os.fdopen(fd, "wb") as file:
                    file.write(Fernet.generate_key())
                    file.flush()
                    os.fsync(file.fileno())
            fd = os.open(self.key_path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as file:
                os.fchmod(file.fileno(), 0o600)
                key = file.read(128)
            try:
                return Fernet(key)
            except ValueError:
                raise ValueError(
                    "Credential key is invalid; restore the original key backup"
                ) from None

    def list(self) -> list[dict]:
        with self._connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"SELECT {_COLUMNS} FROM credentials ORDER BY created_at,id"
                )
            ]

    def _row(self, identity):
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM credentials WHERE id=?", (identity,)
            ).fetchone()
        if row is None:
            raise ValueError("Credential reference does not exist")
        return dict(row)

    @staticmethod
    def _public(row):
        return {key: row[key] for key in _COLUMNS.split(",")}

    @staticmethod
    def _payload(row, secret):
        return json.dumps(
            {key: row[key] for key in ("id", "kind", "origin", "header_name")}
            | {"secret": secret}
        ).encode()

    def create(self, name: str, kind: str, origin: str, secret: str, header_name=""):
        secret = _secret(secret)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
            raise ValueError("Credential name must contain 1–120 characters")
        if kind not in {"bearer", "api_key"}:
            raise ValueError("Unsupported credential kind")
        normalized = credential_origin(origin)
        parsed = urlparse(origin)
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("Credential origin must contain only the HTTPS host")
        if kind == "bearer":
            header_name = "Authorization"
        elif (
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", header_name)
            or header_name.lower() in _RESERVED
            or header_name.lower().startswith("proxy-")
        ):
            raise ValueError("API key header must be a non-reserved HTTP header name")
        now = datetime.now(timezone.utc).isoformat()
        row = dict(
            id=str(uuid.uuid4()),
            name=name.strip(),
            kind=kind,
            origin=normalized,
            header_name=header_name,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        cipher = self._cipher()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO credentials VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    row["name"],
                    kind,
                    normalized,
                    header_name,
                    cipher.encrypt(self._payload(row, secret)),
                    1,
                    now,
                    now,
                ),
            )
        return self._public(row)

    @contextmanager
    def bound(self, identity: str, url: str, kinds: tuple[str, ...]):
        with self._lock(identity):
            row = self._row(identity)
            if row["kind"] not in kinds or row["origin"] != credential_origin(url):
                raise ValueError(
                    "Credential kind or HTTPS origin does not match the source"
                )
            yield row

    def material(self, row: dict):
        from cryptography.fernet import InvalidToken

        cipher = self._cipher()
        try:
            payload = json.loads(cipher.decrypt(row["sealed"]))
            if any(
                payload.get(key) != row[key]
                for key in ("id", "kind", "origin", "header_name")
            ):
                raise ValueError("Invalid binding")
            secret = _secret(payload["secret"])
        except (InvalidToken, ValueError, KeyError, TypeError):
            raise ValueError(
                "Credential could not be unlocked; check the original key backup"
            ) from None
        value = f"Bearer {secret}" if row["kind"] == "bearer" else secret
        return {
            "headers": {row["header_name"]: value},
            "origin": row["origin"],
            "secret": secret,
        }

    def rotate(self, identity: str, revision: int, secret: str):
        secret = _secret(secret)
        with self._lock(identity, exclusive=True):
            row = self._row(identity)
            if row["revision"] != revision:
                raise SourceConflict("Credential changed; refresh before rotating")
            # Check the original key/binding before replacing a value.
            self.material(row)
            sealed = self._cipher().encrypt(self._payload(row, secret))
            now = datetime.now(timezone.utc).isoformat()
            with self._connection() as conn:
                conn.execute(
                    "UPDATE credentials SET sealed=?,revision=revision+1,updated_at=? "
                    "WHERE id=?",
                    (sealed, now, identity),
                )
            return self._public(self._row(identity))

    def delete(self, identity: str, revision: int, sources):
        with self._lock(identity, exclusive=True):
            row = self._row(identity)
            if row["revision"] != revision:
                raise SourceConflict("Credential changed; refresh before removing")
            if any(
                source["config"].get("credential_id") == identity
                for source in sources.list()
            ):
                raise SourceConflict(
                    "Credential is attached to a source; detach it before removing"
                )
            with self._connection() as conn:
                conn.execute("DELETE FROM credentials WHERE id=?", (identity,))
