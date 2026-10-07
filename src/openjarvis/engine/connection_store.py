"""Versioned model-server configuration; discovery never enables inference."""

from __future__ import annotations

import ipaddress
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit


class ConnectionConflict(ValueError):
    """The record changed since it was displayed."""


def adapter_definition():
    """Trusted, versioned adapter metadata; discovery grants no tool authority."""
    return {
        "adapter_id": "ollama",
        "display_name": "Ollama",
        "config_version": 1,
        "operations": ["catalog_discovery", "capability_discovery", "chat_enable"],
        "required_capabilities": [],
        "requires_administrator": True,
        "settings": [
            {
                "name": "url",
                "label": "Ollama server URL",
                "type": "text",
                "required": True,
                "max_length": 2048,
                "description": "HTTP(S) root URL with loopback or explicit "
                "private LAN IP.",
                "example": "http://192.168.1.20:11434",
            }
        ],
    }


def definition(name: str, url: str) -> dict:
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", name):
        raise ValueError(
            "Name must start with a lowercase letter and contain 1–24 lowercase "
            "letters, digits or underscores. Example: home_gpu."
        )
    try:
        if not isinstance(url, str) or len(url) > 2048 or not url.isascii():
            raise ValueError
        if any(char.isspace() or ord(char) < 32 for char in url):
            raise ValueError
        parsed = urlsplit(url)
        host = parsed.hostname
        address = ipaddress.ip_address("127.0.0.1" if host == "localhost" else host)
        local = address.is_loopback or any(
            address in ipaddress.ip_network(net)
            for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
            if address.version == ipaddress.ip_network(net).version
        )
        if (
            not local
            or parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "?" in url
            or "#" in url
            or parsed.port == 0
        ):
            raise ValueError
        host = f"[{address}]" if address.version == 6 else str(address)
        port = f":{parsed.port}" if parsed.port is not None else ""
    except (ValueError, TypeError):
        raise ValueError(
            "Use an HTTP(S) root URL with localhost, a loopback IP or an explicit "
            "private LAN IP and port, such as http://192.168.1.20:11434. "
            "Credentials, paths, queries and public addresses are unsupported."
        ) from None
    return {
        "name": name,
        "url": f"{parsed.scheme}://{host}{port}",
        "adapter_id": "ollama",
        "config_version": 1,
    }


class ModelConnectionStore:
    def __init__(self, path):
        self.path = Path(path).expanduser().absolute()
        if any(part.is_symlink() for part in (self.path, *self.path.parents)):
            raise ValueError("Model connection storage must not use symbolic links")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5, 6):
                raise ValueError("Unsupported model connection database version")
            if version == 0:
                db.execute("""CREATE TABLE model_connections (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                    url TEXT NOT NULL, adapter_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, catalog TEXT NOT NULL DEFAULT '[]',
                    discovery_state TEXT NOT NULL DEFAULT 'untested',
                    tested_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                )""")
                db.execute("""CREATE TABLE model_connection_audit (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, connection_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, event TEXT NOT NULL,
                    actor TEXT NOT NULL, timestamp REAL NOT NULL
                )""")
                db.execute("PRAGMA user_version=1")
            if version < 2:
                db.execute(
                    "ALTER TABLE model_connections ADD COLUMN "
                    "config_version INTEGER NOT NULL DEFAULT 1"
                )
                db.execute("PRAGMA user_version=2")
            if version < 3:
                db.execute(
                    "ALTER TABLE model_connections ADD COLUMN "
                    "enabled INTEGER NOT NULL DEFAULT 0"
                )
                db.execute("PRAGMA user_version=3")
            if version < 4:
                db.execute("""CREATE TABLE IF NOT EXISTS model_benchmarks (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                    connection_id TEXT NOT NULL, connection_revision INTEGER NOT NULL,
                    model_id TEXT NOT NULL, task TEXT NOT NULL,
                    suite_version TEXT NOT NULL,
                    passed INTEGER NOT NULL, details TEXT NOT NULL,
                    elapsed_ms REAL NOT NULL, tokens INTEGER NOT NULL,
                    actor TEXT NOT NULL, timestamp REAL NOT NULL
                )""")
                db.execute("""CREATE TABLE IF NOT EXISTS model_task_rules (
                    task TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    model_id TEXT NOT NULL DEFAULT '',
                    benchmark_id TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )""")
                db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_model_benchmark_latest "
                    "ON model_benchmarks(model_id,task,seq DESC)"
                )
                db.execute("""CREATE TABLE IF NOT EXISTS model_routing_audit (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT NOT NULL,
                    revision INTEGER NOT NULL, event TEXT NOT NULL,
                    actor TEXT NOT NULL, timestamp REAL NOT NULL
                )""")
                for task in ("general", "coding", "analysis", "vision"):
                    db.execute(
                        "INSERT OR IGNORE INTO model_task_rules "
                        "(task,revision,actor,updated_at) VALUES (?,1,'migration',?)",
                        (task, time.time()),
                    )
                db.execute("PRAGMA user_version=4")
            if version < 5:
                columns = {
                    r[1] for r in db.execute("PRAGMA table_info(model_task_rules)")
                }
                for column in ("fallback_model_id", "fallback_benchmark_id"):
                    if column not in columns:
                        db.execute(
                            f"ALTER TABLE model_task_rules ADD COLUMN {column} "
                            "TEXT NOT NULL DEFAULT ''"
                        )
                db.execute("PRAGMA user_version=5")
            if version < 6:
                # Keep bindings after removal so refresh cannot undo an admin choice.
                db.execute("""CREATE TABLE IF NOT EXISTS backend_model_imports (
                    endpoint TEXT PRIMARY KEY, connection_id TEXT NOT NULL,
                    revision INTEGER NOT NULL
                )""")
                db.execute("PRAGMA user_version=6")
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def public(row):
        value = dict(row)
        value["catalog"] = json.loads(value["catalog"])
        value["enabled"] = bool(value["enabled"])
        return value

    def list(self):
        with self.connection() as db:
            return [
                self.public(row)
                for row in db.execute("SELECT * FROM model_connections ORDER BY name")
            ]

    def get(self, identity, revision=None):
        with self.connection() as db:
            return self.public(self._row(db, identity, revision))

    @staticmethod
    def _row(db, identity, revision):
        row = db.execute(
            "SELECT * FROM model_connections WHERE id=?", (identity,)
        ).fetchone()
        if row is None:
            raise KeyError(identity)
        if revision is not None and row["revision"] != revision:
            raise ConnectionConflict("Connection changed. Reload and try again.")
        return row

    @staticmethod
    def _audit(db, identity, revision, event, actor):
        db.execute(
            "INSERT INTO model_connection_audit "
            "(connection_id,revision,event,actor,timestamp) VALUES (?,?,?,?,?)",
            (identity, revision, event, actor, time.time()),
        )

    def create(self, name, url, actor):
        settings = definition(name, url)
        identity, now = uuid.uuid4().hex, time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM model_connections").fetchone()[0] >= 64:
                raise ValueError("Maximum of 64 model connections reached")
            db.execute(
                "INSERT INTO model_connections "
                "(id,name,url,adapter_id,revision,created_at,updated_at) "
                "VALUES (?,?,?,?,1,?,?)",
                (identity, settings["name"], settings["url"], "ollama", now, now),
            )
            self._audit(db, identity, 1, "created", actor)
            return self.public(self._row(db, identity, None))

    def update(self, identity, revision, name, url, actor):
        settings = definition(name, url)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._row(db, identity, revision)
            db.execute(
                "UPDATE model_connections SET name=?,url=?,revision=revision+1,"
                "catalog='[]',discovery_state='untested',tested_at=0,enabled=0,"
                "updated_at=? "
                "WHERE id=?",
                (settings["name"], settings["url"], time.time(), identity),
            )
            self._audit(db, identity, revision + 1, "edited", actor)
            return self.public(self._row(db, identity, None))

    def discovered(self, identity, revision, catalog, success, actor):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._row(db, identity, revision)
            db.execute(
                "UPDATE model_connections SET catalog=?,discovery_state=?,"
                "tested_at=?,updated_at=?,enabled=0,revision=revision+1 WHERE id=?",
                (
                    json.dumps(catalog if success else [], allow_nan=False),
                    "discovered" if success else "error",
                    time.time(),
                    time.time(),
                    identity,
                ),
            )
            self._audit(
                db,
                identity,
                revision + 1,
                "discovered" if success else "test_failed",
                actor,
            )
            return self.public(self._row(db, identity, None))

    def model_capabilities(self, identity, revision, serving_id, capabilities, actor):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, identity, revision)
            catalog = json.loads(row["catalog"])
            model = next((m for m in catalog if m["serving_id"] == serving_id), None)
            if model is None:
                raise ValueError(
                    "Model is not in this connection's catalog. Test catalog again."
                )
            model["capabilities"] = capabilities or []
            model["capability_state"] = (
                "reported" if capabilities is not None else "unknown"
            )
            model["capabilities_at"] = time.time() if capabilities is not None else 0
            db.execute(
                "UPDATE model_connections SET catalog=?,revision=revision+1,"
                "enabled=0,updated_at=? WHERE id=?",
                (json.dumps(catalog, allow_nan=False), time.time(), identity),
            )
            self._audit(
                db,
                identity,
                revision + 1,
                "capabilities_read"
                if capabilities is not None
                else "capabilities_failed",
                actor,
            )
            return self.public(self._row(db, identity, None))

    def enable(self, identity, revision, enabled, actor):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, identity, revision)
            if enabled and (
                row["adapter_id"] != "ollama"
                or row["config_version"] != 1
                or row["discovery_state"] != "discovered"
                or not any(
                    m.get("capability_state") == "reported"
                    and "completion" in m.get("capabilities", [])
                    for m in json.loads(row["catalog"])
                )
            ):
                raise ValueError(
                    "Test catalog and read capabilities for a chat model "
                    "before enabling."
                )
            db.execute(
                "UPDATE model_connections SET enabled=?,revision=revision+1,"
                "updated_at=? WHERE id=?",
                (int(enabled), time.time(), identity),
            )
            self._audit(
                db, identity, revision + 1, "enabled" if enabled else "disabled", actor
            )
            return self.public(self._row(db, identity, None))

    def remove(self, identity, revision, actor):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._row(db, identity, revision)
            db.execute("DELETE FROM model_connections WHERE id=?", (identity,))
            self._audit(db, identity, revision, "removed", actor)

    def audit(self, identity):
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT revision,event,actor,timestamp FROM model_connection_audit "
                    "WHERE connection_id=? ORDER BY seq",
                    (identity,),
                )
            ]

    def backend_target(self, endpoint, db=None):
        """Snapshot the import binding and any explicit administrator override."""
        if db is None:
            with self.connection() as db:
                return self.backend_target(endpoint, db)
        binding = db.execute(
            "SELECT * FROM backend_model_imports WHERE endpoint=?", (endpoint,)
        ).fetchone()
        if binding:
            row = db.execute(
                "SELECT * FROM model_connections WHERE id=?",
                (binding["connection_id"],),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM model_connections WHERE url=? "
                "ORDER BY EXISTS(SELECT 1 FROM model_connection_audit "
                "WHERE connection_id=model_connections.id AND "
                "event IN ('enabled','disabled','edited')) DESC,created_at LIMIT 1",
                (endpoint,),
            ).fetchone()
        managed = bool(binding and (not row or row["revision"] != binding["revision"]))
        if row and (row["adapter_id"] != "ollama" or row["config_version"] != 1):
            managed = True
        if row and not binding:
            managed = managed or bool(
                db.execute(
                    "SELECT 1 FROM model_connection_audit WHERE connection_id=? "
                    "AND event IN ('enabled','disabled','edited') LIMIT 1",
                    (row["id"],),
                ).fetchone()
            )
        return {
            "binding": dict(binding) if binding else None,
            "connection": self.public(row) if row else None,
            "managed": managed,
        }

    def inherit_backend(self, endpoint, catalog, expected, actor):
        """Atomically inherit an active backend without overwriting admin changes."""
        endpoint = definition("backend_ollama", endpoint)["url"]
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self.backend_target(endpoint, db)
            if current != expected:
                raise ConnectionConflict("Connection changed. Refresh and try again.")
            if current["managed"]:
                return False
            row = current["connection"]
            now = time.time()
            enabled = any(
                m.get("capability_state") == "reported"
                and "completion" in m.get("capabilities", [])
                for m in catalog
            )

            def stable(items):
                return sorted(
                    [
                        {k: v for k, v in m.items() if k != "capabilities_at"}
                        for m in items
                    ],
                    key=lambda m: m["serving_id"],
                )

            if row and row["adapter_id"] != "ollama":
                return False
            if (
                row
                and stable(row["catalog"]) == stable(catalog)
                and row["discovery_state"] == "discovered"
                and row["enabled"] == enabled
                and row["config_version"] == 1
            ):
                db.execute(
                    "INSERT OR IGNORE INTO backend_model_imports VALUES (?,?,?)",
                    (endpoint, row["id"], row["revision"]),
                )
                return False
            if not row:
                if (
                    db.execute("SELECT COUNT(*) FROM model_connections").fetchone()[0]
                    >= 64
                ):
                    raise ValueError("Maximum of 64 model connections reached")
                names = {r[0] for r in db.execute("SELECT name FROM model_connections")}
                name = next(
                    n
                    for n in ["backend_ollama"]
                    + [f"backend_ollama_{i}" for i in range(1, 65)]
                    if n not in names
                )
                identity, revision = uuid.uuid4().hex, 1
                db.execute(
                    "INSERT INTO model_connections "
                    "(id,name,url,adapter_id,revision,created_at,updated_at) "
                    "VALUES (?,?,?,'ollama',?,?,?)",
                    (identity, name, endpoint, revision, now, now),
                )
            else:
                identity, revision = row["id"], row["revision"] + 1
            db.execute(
                "UPDATE model_connections SET catalog=?,discovery_state='discovered',"
                "enabled=?,revision=?,tested_at=?,updated_at=? WHERE id=?",
                (
                    json.dumps(catalog, allow_nan=False),
                    int(enabled),
                    revision,
                    now,
                    now,
                    identity,
                ),
            )
            db.execute(
                "INSERT OR REPLACE INTO backend_model_imports VALUES (?,?,?)",
                (endpoint, identity, revision),
            )
            self._audit(db, identity, revision, "backend_inherited", actor)
            return True
