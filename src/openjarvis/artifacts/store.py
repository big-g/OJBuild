"""Immutable, bounded file storage with verified owner lookups."""

from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_USER_BYTES = 200 * 1024 * 1024
MAX_USER_FILES = 100
MAX_REQUEST_BYTES = 28 * 1024 * 1024


class ArtifactError(ValueError):
    """Invalid input, quota or corrupt backing file."""


class ArtifactNotFound(ArtifactError):
    """Absent file or different owner; deliberately indistinguishable."""


@contextmanager
def _directory(path: Path):
    """Walk every directory without following symlinks, including ancestors."""
    path = path.absolute()
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


class ArtifactStore:
    def __init__(self, root: str | Path):
        if not all(
            hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
        ):
            raise NotImplementedError(
                "Private file storage requires a POSIX server filesystem"
            )
        self.root = Path(root).expanduser().absolute()
        with _directory(self.root) as fd:
            os.fchmod(fd, 0o700)
            catalog = os.open(
                "catalog.db", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=fd
            )
            try:
                if (
                    not stat.S_ISREG(os.fstat(catalog).st_mode)
                    or os.fstat(catalog).st_nlink != 1
                ):
                    raise ArtifactError("Unsafe file catalog")
                os.fchmod(catalog, 0o600)
            finally:
                os.close(catalog)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, filename TEXT NOT NULL,
                size INTEGER NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL
            )""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS artifact_owner ON artifacts(owner_id)"
            )
        from openjarvis.security.file_policy import protect_directory

        protect_directory(self.root)

    @contextmanager
    def _db(self):
        # The root is server-private; reject replaced catalog/ancestor symlinks.
        with _directory(self.root) as fd:
            info = os.stat("catalog.db", dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ArtifactError("Unsafe file catalog")
            db = sqlite3.connect(self.root / "catalog.db", timeout=10)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    @staticmethod
    def _owner(owner: str) -> str:
        if not isinstance(owner, str) or not owner or len(owner) > 256:
            raise ArtifactError("An authenticated user is required")
        return hashlib.sha256(owner.encode()).hexdigest()

    @staticmethod
    def _id(file_id: str) -> str:
        try:
            if str(uuid.UUID(file_id)) != file_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise ArtifactNotFound("File not found") from exc
        return file_id

    def save(
        self, owner: str, filename: str, content: str, encoding: str = "utf8"
    ) -> dict:
        owner_dir = self._owner(owner)
        if (
            not isinstance(filename, str)
            or not filename.strip()
            or len(filename) > 150
            or filename in (".", "..")
            or any(c in filename for c in "/\\")
            or any(ord(c) < 32 or ord(c) == 127 for c in filename)
        ):
            raise ArtifactError(
                "Use a filename without directories or control characters"
            )
        try:
            filename.encode("utf-8")
        except UnicodeError as exc:
            raise ArtifactError("Invalid filename encoding") from exc
        if not isinstance(content, str) or len(content) > MAX_REQUEST_BYTES:
            raise ArtifactError("Invalid or oversized content")
        try:
            if encoding == "utf8":
                data = content.encode("utf-8")
            elif encoding == "base64":
                data = base64.b64decode(content, validate=True)
            else:
                raise ArtifactError("Encoding must be utf8 or base64")
        except (ValueError, UnicodeError) as exc:
            raise ArtifactError("Invalid file encoding") from exc
        if len(data) > MAX_FILE_BYTES:
            raise ArtifactError("Files are limited to 20 MiB")
        row = dict(
            id=str(uuid.uuid4()),
            owner_id=owner,
            filename=filename,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._db() as db, _directory(self.root / owner_dir) as fd:
            os.fchmod(fd, 0o700)
            db.execute("BEGIN IMMEDIATE")
            count, size = db.execute(
                "SELECT count(*), coalesce(sum(size),0) FROM artifacts "
                "WHERE owner_id=?",
                (owner,),
            ).fetchone()
            if count >= MAX_USER_FILES or size + len(data) > MAX_USER_BYTES:
                raise ArtifactError("User storage quota exceeded (100 files / 200 MiB)")
            blob = row["id"] + ".blob"
            created = False
            try:
                handle = os.open(
                    blob,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=fd,
                )
                created = True
                with os.fdopen(handle, "wb") as out:
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
                db.execute(
                    "INSERT INTO artifacts VALUES (?,?,?,?,?,?)", tuple(row.values())
                )
                db.commit()
            except BaseException:
                if created:
                    os.unlink(blob, dir_fd=fd)
                raise
        return self._public(row)

    @staticmethod
    def _public(row) -> dict:
        return {
            key: row[key] for key in ("id", "filename", "size", "sha256", "created_at")
        }

    def list(self, owner: str) -> list[dict]:
        self._owner(owner)
        with self._db() as db:
            return [
                self._public(row)
                for row in db.execute(
                    "SELECT * FROM artifacts WHERE owner_id=? ORDER BY created_at DESC",
                    (owner,),
                )
            ]

    def read(self, owner: str, file_id: str) -> tuple[dict, bytes]:
        file_id = self._id(file_id)
        folder = self._owner(owner)
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM artifacts WHERE owner_id=? AND id=?", (owner, file_id)
            ).fetchone()
        if row is None:
            raise ArtifactNotFound("File not found")
        try:
            with _directory(self.root / folder) as fd:
                handle = os.open(
                    file_id + ".blob",
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=fd,
                )
                with os.fdopen(handle, "rb") as source:
                    info = os.fstat(source.fileno())
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_nlink != 1
                        or info.st_size != row["size"]
                        or info.st_size > MAX_FILE_BYTES
                    ):
                        raise ArtifactError("Stored file failed integrity validation")
                    data = source.read(MAX_FILE_BYTES + 1)
        except OSError as exc:
            raise ArtifactError("Stored file is unavailable") from exc
        if (
            len(data) != row["size"]
            or hashlib.sha256(data).hexdigest() != row["sha256"]
        ):
            raise ArtifactError("Stored file failed integrity validation")
        return self._public(row), data

    def delete(self, owner: str, file_id: str) -> None:
        file_id = self._id(file_id)
        folder = self._owner(owner)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM artifacts WHERE id=? AND owner_id=?", (file_id, owner)
            ).fetchone()
            if row is None:
                raise ArtifactNotFound("File not found")
            with _directory(self.root / folder) as fd:
                try:
                    os.unlink(file_id + ".blob", dir_fd=fd)
                except FileNotFoundError:
                    pass
            db.execute(
                "DELETE FROM artifacts WHERE id=? AND owner_id=?", (file_id, owner)
            )
