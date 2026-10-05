"""Atomic MCP configuration, encrypted credentials, catalog reviews and audit."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path

from openjarvis.connectors.source_credentials import CredentialStore, _secret
from openjarvis.mcp.network import NETWORK_FIELDS, endpoint, policy
from openjarvis.tools.runtime_store import RuntimeToolConflict

VERSION = "runtime-mcp-v1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def definition(value):
    if (
        not isinstance(value, dict)
        or not {"name", "url"} <= set(value)
        or set(value) - {"name", "url", "allow_without_confirmation"} - NETWORK_FIELDS
    ):
        raise ValueError("Expected connection name and HTTPS URL")
    if not isinstance(value["name"], str) or not re.fullmatch(
        r"[a-z][a-z0-9_]{0,23}",
        value["name"],
    ):
        raise ValueError("Connection name must use 1–24 lowercase letters/digits")
    automatic = value.get("allow_without_confirmation", False)
    if not isinstance(automatic, bool):
        raise ValueError("Execution confirmation setting must be boolean")
    settings = policy(value)
    return {
        "name": value["name"],
        "url": endpoint(value["url"], settings),
        **(settings if settings["network_access"] == "lan" else {}),
        "allow_without_confirmation": automatic,
    }


def fingerprint(row):
    return hashlib.sha256(
        canonical(
            {
                "id": row["id"],
                "definition": json.loads(row["definition"]),
                "token_revision": row["token_revision"],
                "catalog": json.loads(row["catalog"]),
                "validator": VERSION,
            }
        ).encode()
    ).hexdigest()


class RuntimeMCPStore:
    def __init__(self, path):
        self.path = Path(path).expanduser().absolute()
        if any(part.is_symlink() for part in (self.path, *self.path.parents)):
            raise ValueError("MCP storage paths must not contain symbolic links")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.vault = CredentialStore(self.path)
        with self.vault._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runtime_mcp (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                    definition TEXT NOT NULL, revision INTEGER NOT NULL,
                    token_revision INTEGER NOT NULL DEFAULT 0,
                    catalog TEXT NOT NULL DEFAULT '[]',
                    discovered INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    approved_fingerprint TEXT NOT NULL DEFAULT '',
                    approved_by TEXT NOT NULL DEFAULT '',
                    approved_at REAL NOT NULL DEFAULT 0,
                    validator_version TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_mcp_audit (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    connection_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    event TEXT NOT NULL, actor TEXT NOT NULL, timestamp REAL NOT NULL
                );
            """)

    def get(self, identity):
        with self.vault._connection() as db:
            row = db.execute(
                "SELECT * FROM runtime_mcp WHERE id=?",
                (identity,),
            ).fetchone()
        if row is None:
            raise KeyError(identity)
        return dict(row)

    def list(self):
        with self.vault._connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM runtime_mcp ORDER BY name",
                )
            ]

    def audit(self, identity):
        with self.vault._connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM runtime_mcp_audit WHERE connection_id=? "
                    "ORDER BY seq DESC LIMIT 100",
                    (identity,),
                )
            ]

    @staticmethod
    def _audit(db, identity, revision, event, actor):
        db.execute(
            "INSERT INTO runtime_mcp_audit"
            "(connection_id,revision,event,actor,timestamp) VALUES(?,?,?,?,?)",
            (identity, revision, event, actor, time.time()),
        )

    def _seal(self, identity, config, token):
        if token is None:
            return None
        if not isinstance(token, str):
            raise ValueError("Invalid bearer token")
        if not token:
            return b""
        _secret(token)
        if token in config["url"]:
            raise ValueError("Endpoint URL must not contain the bearer token")
        return self.vault._cipher().encrypt(
            canonical(
                {
                    "id": identity,
                    "url": config["url"],
                    "token": token,
                }
            ).encode()
        )

    def token(self, row):
        if not row["token_revision"]:
            return ""
        from cryptography.fernet import InvalidToken

        with self.vault._connection() as db:
            saved = db.execute(
                "SELECT * FROM connector_tokens WHERE id=?",
                (row["id"],),
            ).fetchone()
        if saved is None or saved["revision"] != row["token_revision"]:
            raise ValueError("MCP credential changed or is unavailable")
        try:
            value = json.loads(self.vault._cipher().decrypt(saved["sealed"]))
            if (
                value["id"] != row["id"]
                or value["url"] != json.loads(row["definition"])["url"]
            ):
                raise ValueError("Invalid binding")
            return _secret(value["token"])
        except (InvalidToken, ValueError, TypeError, KeyError):
            raise ValueError("MCP credential could not be unlocked") from None

    @staticmethod
    def _credential(db, identity, sealed, revision):
        if sealed is None:
            return
        if not sealed:
            db.execute("DELETE FROM connector_tokens WHERE id=?", (identity,))
        else:
            db.execute(
                "INSERT INTO connector_tokens VALUES(?,?,?) ON CONFLICT(id) "
                "DO UPDATE SET sealed=excluded.sealed,revision=excluded.revision",
                (identity, sealed, revision),
            )

    def create(self, config, actor, token="", *, event="created"):
        config = definition(config)
        identity = str(uuid.uuid4())
        sealed = self._seal(identity, config, token)
        with self.vault._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM runtime_mcp").fetchone()[0] >= 24:
                raise ValueError("MCP connection limit reached (24)")
            try:
                db.execute(
                    "INSERT INTO runtime_mcp"
                    "(id,name,definition,revision,token_revision,validator_version) "
                    "VALUES(?,?,?,1,?,?)",
                    (
                        identity,
                        config["name"],
                        canonical(config),
                        bool(sealed),
                        VERSION,
                    ),
                )
            except sqlite3.IntegrityError:
                raise RuntimeToolConflict("Connection name is already saved") from None
            self._credential(db, identity, sealed, 1)
            self._audit(db, identity, 1, event, actor)
        return self.get(identity)

    def change(
        self,
        identity,
        revision,
        event,
        actor,
        *,
        config=None,
        token=None,
        catalog=None,
    ):
        old = self.get(identity)
        if token and old["token_revision"]:
            self.token(old)  # Do not overwrite ciphertext under a missing/wrong key.
        if config is not None:
            config = definition(config)
            # A kept token is rebound only by providing a new token. A changed
            # endpoint cannot silently receive an existing endpoint's secret.
            try:
                previous_config = json.loads(old["definition"])
                previous_url = previous_config["url"]
                previous_policy = policy(previous_config)
            except (ValueError, KeyError, TypeError):
                previous_url = None
                previous_policy = None
            if token is None and (
                config["url"] != previous_url or policy(config) != previous_policy
            ):
                token = ""
        sealed = (
            self._seal(identity, config or json.loads(old["definition"]), token)
            if token is not None
            else None
        )
        with self.vault._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM runtime_mcp WHERE id=?",
                (identity,),
            ).fetchone()
            if row is None:
                raise KeyError(identity)
            if row["revision"] != revision:
                raise RuntimeToolConflict("Connection changed; reload before reviewing")
            row = dict(row)
            row["revision"] += 1
            if event == "updated":
                row.update(
                    name=config["name"],
                    definition=canonical(config),
                    catalog="[]",
                    discovered=0,
                    enabled=0,
                    approved_fingerprint="",
                    approved_by="",
                    approved_at=0,
                )
                if sealed is not None:
                    row["token_revision"] = revision + 1 if sealed else 0
                    self._credential(db, identity, sealed, row["token_revision"])
            elif event in {"discovered", "discovery_failed"}:
                row["catalog"] = canonical(catalog or [])
                row["discovered"] = event == "discovered"
                if (
                    fingerprint(row) != row["approved_fingerprint"]
                    or not row["discovered"]
                ):
                    row.update(
                        enabled=0,
                        approved_fingerprint="",
                        approved_by="",
                        approved_at=0,
                    )
            elif event == "approved":
                if not row["discovered"] or not json.loads(row["catalog"]):
                    raise ValueError("Discover a nonempty catalog before approving")
                row.update(
                    approved_fingerprint=fingerprint(row),
                    approved_by=actor,
                    approved_at=time.time(),
                    enabled=1,
                    validator_version=VERSION,
                )
            elif event in {"enabled", "disabled"}:
                if event == "enabled" and not self.approved(row):
                    raise ValueError("Connection needs fresh catalog approval")
                row["enabled"] = event == "enabled"
            elif event == "deleted":
                db.execute("DELETE FROM runtime_mcp WHERE id=?", (identity,))
                self._credential(db, identity, b"", 0)
            else:
                raise ValueError("Unknown MCP connection operation")
            if event != "deleted":
                columns = [key for key in row if key != "id"]
                try:
                    db.execute(
                        "UPDATE runtime_mcp SET "
                        + ",".join(f"{key}=?" for key in columns)
                        + " WHERE id=?",
                        [row[key] for key in columns] + [identity],
                    )
                except sqlite3.IntegrityError:
                    raise RuntimeToolConflict(
                        "Connection name is already saved"
                    ) from None
            self._audit(db, identity, revision + 1, event, actor)
        return None if event == "deleted" else self.get(identity)

    @staticmethod
    def approved(row):
        return bool(
            row["discovered"]
            and row["validator_version"] == VERSION
            and row["approved_fingerprint"] == fingerprint(row)
        )
