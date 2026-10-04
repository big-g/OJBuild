"""Persistent, revision-bound runtime tool definitions and approval decisions."""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from openjarvis.connectors._sqlite import initialize_wal
from openjarvis.tools.runtime_adapters import TRANSFORMS as TRANSFORMS
from openjarvis.tools.runtime_adapters import (
    adapter_config,
    get_adapter,
)

# Retained for callers that use the original transform-only API.
VALIDATOR_VERSION = "transform-v1"


class RuntimeToolConflict(ValueError):
    """The reviewed revision is no longer current."""


def validate_definition(definition: dict) -> dict:
    """Adapters validate config; callers cannot supply an execution contract."""
    if not isinstance(definition, dict) or set(definition) not in (
        {"name", "description", "transform"},
        {"name", "description", "adapter_id", "config"},
    ):
        raise ValueError("Expected name, description and one adapter configuration")
    name = definition["name"]
    if not isinstance(name, str) or not re.fullmatch(
        r"custom_[a-z][a-z0-9_]{0,55}", name
    ):
        raise ValueError("Tool names must start with custom_ and use lowercase letters")
    description = definition["description"]
    if (
        not isinstance(description, str)
        or not description.strip()
        or len(description) > 500
    ):
        raise ValueError("Description must contain 1–500 characters")
    get_adapter(definition).validate(adapter_config(definition))
    from openjarvis.core.registry import ToolRegistry

    if ToolRegistry.contains(name):
        raise ValueError("Tool name is already registered")
    return dict(definition)


class RuntimeToolStore:
    """Short-lived SQLite connections support threads and multiple processes."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            initialize_wal(db)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runtime_tools (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    definition TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    approved_fingerprint TEXT NOT NULL DEFAULT '',
                    approved_by TEXT NOT NULL DEFAULT '',
                    approved_at REAL NOT NULL DEFAULT 0,
                    validator_version TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_tool_audit (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    tool_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    event TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    timestamp REAL NOT NULL
                );
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, identity: str) -> dict:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM runtime_tools WHERE id=?", (identity,)
            ).fetchone()
        if row is None:
            raise KeyError(identity)
        return dict(row)

    def list(self) -> list[dict]:
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute("SELECT * FROM runtime_tools ORDER BY name")
            ]

    def audit(self, identity: str) -> list[dict]:
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM runtime_tool_audit WHERE tool_id=? "
                    "ORDER BY seq DESC LIMIT 100",
                    (identity,),
                )
            ]

    @staticmethod
    def _audit(db, identity, revision, event, actor):
        db.execute(
            "INSERT INTO runtime_tool_audit(tool_id,revision,event,actor,timestamp) "
            "VALUES(?,?,?,?,?)",
            (identity, revision, event, actor, time.time()),
        )

    def create(self, definition: dict, actor: str) -> dict:
        definition = validate_definition(definition)
        identity = str(uuid.uuid4())
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM runtime_tools").fetchone()[0] >= 100:
                raise ValueError("Runtime tool limit reached (100)")
            try:
                db.execute(
                    "INSERT INTO runtime_tools"
                    "(id,name,definition,revision,validator_version) "
                    "VALUES(?,?,?,?,?)",
                    (
                        identity,
                        definition["name"],
                        json.dumps(definition),
                        1,
                        get_adapter(definition).validator_version,
                    ),
                )
            except sqlite3.IntegrityError:
                raise RuntimeToolConflict("Tool name is already installed") from None
            self._audit(db, identity, 1, "created", actor)
        return self.get(identity)

    def change(
        self, identity, revision, event, actor, *, definition=None, fingerprint=""
    ):
        if definition is not None:
            definition = validate_definition(definition)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM runtime_tools WHERE id=?", (identity,)
            ).fetchone()
            if row is None:
                raise KeyError(identity)
            if row["revision"] != revision:
                raise RuntimeToolConflict("Tool changed; reload before reviewing")
            next_revision = revision + 1
            if event == "updated":
                try:
                    db.execute(
                        "UPDATE runtime_tools SET name=?,definition=?,revision=?,"
                        "enabled=0,"
                        "approved_fingerprint='',approved_by='',approved_at=0,"
                        "validator_version=? WHERE id=?",
                        (
                            definition["name"],
                            json.dumps(definition),
                            next_revision,
                            get_adapter(definition).validator_version,
                            identity,
                        ),
                    )
                except sqlite3.IntegrityError:
                    raise RuntimeToolConflict(
                        "Tool name is already installed"
                    ) from None
            elif event == "approved":
                if not fingerprint:
                    raise ValueError("Approval requires a validated fingerprint")
                db.execute(
                    "UPDATE runtime_tools SET revision=?,enabled=1,"
                    "approved_fingerprint=?,"
                    "approved_by=?,approved_at=?,validator_version=? WHERE id=?",
                    (
                        next_revision,
                        fingerprint,
                        actor,
                        time.time(),
                        get_adapter(json.loads(row["definition"])).validator_version,
                        identity,
                    ),
                )
            elif event in {"disabled", "enabled"}:
                if event == "enabled" and (
                    not fingerprint
                    or fingerprint != row["approved_fingerprint"]
                    or row["validator_version"]
                    != get_adapter(json.loads(row["definition"])).validator_version
                ):
                    raise ValueError("Tool needs approval before enabling")
                db.execute(
                    "UPDATE runtime_tools SET revision=?,enabled=? WHERE id=?",
                    (next_revision, event == "enabled", identity),
                )
            elif event == "deleted":
                db.execute("DELETE FROM runtime_tools WHERE id=?", (identity,))
            else:
                raise ValueError("Unknown runtime tool operation")
            self._audit(db, identity, next_revision, event, actor)
        return None if event == "deleted" else self.get(identity)
