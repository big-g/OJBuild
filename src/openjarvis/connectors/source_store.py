"""Persistent named source configuration with versioning and optimistic edits."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from openjarvis.core.config import DEFAULT_CONFIG_DIR


class SourceConflict(ValueError):
    """A source changed or is busy; the client must refresh before retrying."""


class SourceStore:
    def __init__(self, db_path: str = "", *, legacy_path: str = "") -> None:
        self.path = Path(db_path) if db_path else DEFAULT_CONFIG_DIR / "sources.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise ValueError("Source database uses an unsupported schema version")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, adapter_id TEXT NOT NULL,
                    name TEXT NOT NULL, config TEXT NOT NULL,
                    config_version INTEGER NOT NULL, revision INTEGER NOT NULL,
                    enabled INTEGER NOT NULL, legacy_document_ids INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'idle', error TEXT
                );
                CREATE TABLE IF NOT EXISTS source_migrations (
                    name TEXT PRIMARY KEY
                );
                PRAGMA user_version=1;
            """)
        legacy = (
            Path(legacy_path)
            if legacy_path
            else DEFAULT_CONFIG_DIR / "connectors" / "local_files.json"
        )
        self._migrate_local_files(legacy)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _record(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["config"] = json.loads(result["config"])
        result["enabled"] = bool(result["enabled"])
        result["legacy_document_ids"] = bool(result["legacy_document_ids"])
        return result

    def list(self) -> list[dict]:
        with self.connection() as conn:
            return [
                self._record(row)
                for row in conn.execute("SELECT * FROM sources ORDER BY created_at, id")
            ]

    def get(self, source_id: str) -> dict:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM sources WHERE id=?", (source_id,)
            ).fetchone()
        if row is None:
            raise KeyError(source_id)
        return self._record(row)

    def create(self, adapter_id: str, name: str, config: dict, version: int) -> dict:
        source_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO sources (id,adapter_id,name,config,config_version,"
                "revision,enabled,legacy_document_ids,created_at,updated_at) "
                "VALUES (?,?,?,?,?,1,1,0,?,?)",
                (source_id, adapter_id, name, json.dumps(config), version, now, now),
            )
        return self.get(source_id)

    def update(
        self, source_id: str, revision: int, *, name: str, config: dict, enabled: bool
    ) -> dict:
        with self.connection() as conn:
            changed = conn.execute(
                "UPDATE sources SET name=?,config=?,enabled=?,revision=revision+1,"
                "updated_at=? WHERE id=? AND revision=?",
                (
                    name,
                    json.dumps(config),
                    enabled,
                    datetime.now(timezone.utc).isoformat(),
                    source_id,
                    revision,
                ),
            ).rowcount
            if not changed:
                raise SourceConflict("Source changed; refresh before saving")
        return self.get(source_id)

    def delete(self, source_id: str) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM sources WHERE id=?", (source_id,))

    def set_status(self, source_id: str, state: str, error: str | None = None) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE sources SET state=?,error=? WHERE id=?",
                (state, error, source_id),
            )

    def _migrate_local_files(self, path: Path) -> None:
        if not path.exists():
            return
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM source_migrations WHERE name='local_files_json'"
            ).fetchone():
                return
            config = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(config.get("path"), str):
                raise ValueError("Legacy Local Files configuration has no directory")
            # Preserve the saved canonical path even when its mount is unavailable.
            # Testing/sync will report that problem; migration must retain the source.
            config = {"path": config["path"]}
            source_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, "openjarvis:legacy:local_files")
            )
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO sources (id,adapter_id,name,config,config_version,"
                "revision,enabled,legacy_document_ids,created_at,updated_at) "
                "VALUES (?,'local_files','Local Files',?,1,1,1,1,?,?)",
                (source_id, json.dumps(config), now, now),
            )
            conn.execute("INSERT INTO source_migrations VALUES ('local_files_json')")
        # Retain a backup. A committed migration marker prevents re-import if the
        # process stops between the database commit and this rename.
        path.replace(path.with_suffix(".json.migrated"))
