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

from openjarvis.connectors._sqlite import initialize_wal
from openjarvis.connectors.source_audit import append_event
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
            initialize_wal(conn)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > 5:
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
                CREATE TABLE IF NOT EXISTS source_imports (
                    name TEXT PRIMARY KEY, source_id TEXT NOT NULL UNIQUE,
                    credential_id TEXT NOT NULL UNIQUE, completed INTEGER NOT NULL
                );

            """)
            from openjarvis.connectors.source_audit import SCHEMA as AUDIT_SCHEMA
            from openjarvis.connectors.source_jobs import SCHEMA

            conn.executescript(SCHEMA + AUDIT_SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
            if "owner_id" not in columns:
                conn.execute(
                    "ALTER TABLE sources ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''"
                )
                conn.execute(
                    "ALTER TABLE sources ADD COLUMN sharing TEXT NOT NULL DEFAULT"
                    " 'personal'"
                )
                conn.execute(
                    "ALTER TABLE sources ADD COLUMN approved_by TEXT NOT NULL "
                    "DEFAULT ''"
                )
                # Historical audit identifies the creator without guessing an owner.
                conn.execute(
                    "UPDATE sources SET owner_id=COALESCE((SELECT substr(actor,6)"
                    " FROM source_audit WHERE source_id=sources.id AND action IN "
                    "('created','imported') AND actor LIKE 'user:%' ORDER BY id "
                    "LIMIT 1),'')"
                )
            if "approved_credential_revision" not in columns:
                conn.execute(
                    "ALTER TABLE sources ADD COLUMN "
                    "approved_credential_revision INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS source_preferences (user_id TEXT "
                "NOT NULL, source_id TEXT NOT NULL REFERENCES sources(id) ON "
                "DELETE CASCADE, enabled INTEGER NOT NULL, PRIMARY "
                "KEY(user_id,source_id))"
            )
            conn.execute("PRAGMA user_version=5")
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
        conn.execute("PRAGMA foreign_keys=ON")
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

    def create(
        self, adapter_id: str, name: str, config: dict, version: int, *, actor="system"
    ) -> dict:
        source_id = str(uuid.uuid4())
        with self.connection() as conn:
            self._insert_source(
                conn, source_id, adapter_id, name, config, version, actor
            )
        return self.get(source_id)

    def _insert_source(
        self,
        conn,
        source_id,
        adapter_id,
        name,
        config,
        version,
        actor,
        *,
        action="created",
    ):
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO sources (id,adapter_id,name,config,config_version,"
            "revision,enabled,legacy_document_ids,created_at,updated_at) "
            "VALUES (?,?,?,?,?,1,1,0,?,?)",
            (source_id, adapter_id, name, json.dumps(config), version, now, now),
        )
        if actor.startswith("user:"):
            conn.execute(
                "UPDATE sources SET owner_id=? WHERE id=?", (actor[5:], source_id)
            )
        record = self._record(
            conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        )
        append_event(
            conn,
            record,
            action,
            actor=actor,
            fields=("name", "config", "enabled"),
        )

    def set_sharing(self, source_id, revision, sharing, *, actor):
        if sharing not in {"personal", "pending", "shared"}:
            raise ValueError("Invalid sharing state")
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if actor.startswith("user:"):
                conn.execute(
                    "UPDATE sources SET owner_id=? WHERE id=? AND owner_id=''",
                    (actor[5:], source_id),
                )
            credential_revision = 0
            if sharing == "shared":
                from openjarvis.connectors.source_credentials import CredentialStore

                row = conn.execute(
                    "SELECT config FROM sources WHERE id=?", (source_id,)
                ).fetchone()
                credential_id = json.loads(row[0]).get("credential_id") if row else None
                if credential_id:
                    credential_revision = CredentialStore(
                        self.path.with_name("source_credentials.db")
                    )._row(credential_id)["revision"]
            changed = conn.execute(
                "UPDATE sources SET "
                "sharing=?,approved_by=?,approved_credential_revision=?,"
                "revision=revision+1,updated_at=? "
                "WHERE id=? AND revision=?",
                (
                    sharing,
                    actor if sharing == "shared" else "",
                    credential_revision,
                    datetime.now(timezone.utc).isoformat(),
                    source_id,
                    revision,
                ),
            ).rowcount
            if not changed:
                raise SourceConflict("Source changed; refresh before sharing")
            record = self._record(
                conn.execute(
                    "SELECT * FROM sources WHERE id=?", (source_id,)
                ).fetchone()
            )
            append_event(
                conn, record, "sharing_" + sharing, actor=actor, fields=("sharing",)
            )
        return self.get(source_id)

    def set_preference(self, user_id, source_id, enabled):
        with self.connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO source_preferences VALUES (?,?,?)",
                (user_id, source_id, enabled),
            )

    def preference(self, user_id, source_id):
        with self.connection() as conn:
            row = conn.execute(
                "SELECT enabled FROM source_preferences WHERE user_id=? AND "
                "source_id=?",
                (user_id, source_id),
            ).fetchone()
        return row is None or bool(row[0])

    def update(
        self,
        source_id: str,
        revision: int,
        *,
        name: str,
        config: dict,
        enabled: bool,
        actor="system",
        config_version=None,
        migration=False,
        index_reset=False,
        legacy_document_ids=None,
    ) -> dict:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM sources WHERE id=?", (source_id,)
            ).fetchone()
            if row is None:
                raise KeyError(source_id)
            old = self._record(row)
            changed = conn.execute(
                "UPDATE sources SET name=?,config=?,enabled=?,revision=revision+1,"
                "updated_at=?,config_version=?,legacy_document_ids=?,sharing="
                "CASE WHEN sharing='shared' THEN 'pending' ELSE sharing "
                "END,approved_by='' "
                "WHERE id=? AND revision=?",
                (
                    name,
                    json.dumps(config),
                    enabled,
                    datetime.now(timezone.utc).isoformat(),
                    config_version
                    if config_version is not None
                    else old["config_version"],
                    legacy_document_ids
                    if legacy_document_ids is not None
                    else old["legacy_document_ids"],
                    source_id,
                    revision,
                ),
            ).rowcount
            if not changed:
                raise SourceConflict("Source changed; refresh before saving")
            record = self._record(
                conn.execute(
                    "SELECT * FROM sources WHERE id=?", (source_id,)
                ).fetchone()
            )
            fields = [
                key
                for key in ("name", "config", "enabled", "config_version")
                if old[key] != record[key]
            ]
            fields.extend(
                f"config.{key}"
                for key in old["config"].keys() | config.keys()
                if old["config"].get(key) != config.get(key)
                or (key in old["config"]) != (key in config)
            )
            if old["legacy_document_ids"] != record["legacy_document_ids"]:
                fields.append("document_identity")
            append_event(
                conn,
                record,
                "migrated" if migration else "updated",
                actor=actor,
                fields=fields,
                index_reset=index_reset,
                previous_version=old["config_version"] if migration else None,
            )
        return self.get(source_id)

    def delete(self, source_id: str, *, actor="system") -> None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM sources WHERE id=?", (source_id,)
            ).fetchone()
            if row is None:
                raise KeyError(source_id)
            append_event(
                conn, self._record(row), "removed", actor=actor, index_reset=True
            )
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
            record = self._record(
                conn.execute(
                    "SELECT * FROM sources WHERE id=?", (source_id,)
                ).fetchone()
            )
            append_event(
                conn, record, "legacy_imported", fields=("config", "name", "enabled")
            )
        # Retain a backup. A committed migration marker prevents re-import if the
        # process stops between the database commit and this rename.
        path.replace(path.with_suffix(".json.migrated"))
