"""Persistent, single-use OAuth browser handoffs and bound callback attempts."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from openjarvis.connectors.source_credentials import CredentialStore


class InvalidOAuthAttempt(ValueError):
    def __init__(self):
        super().__init__("Authorization attempt is invalid or expired; start again")


def fingerprint(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise InvalidOAuthAttempt()
    return hashlib.sha256(value.encode()).hexdigest()


def callback_cookie(state):
    return "oj_oauth_" + fingerprint(state)[:24]


def validate_callback_uri(uri):
    parsed = urlparse(uri)
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or (
            parsed.scheme == "http"
            and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        )
    ):
        raise ValueError("OAuth callbacks require HTTPS or a loopback HTTP address")
    return uri


class OAuthStateStore:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "oauth_attempts.db"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.vault = CredentialStore(directory / "source_credentials.db")
        with self.connection() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise ValueError("Unsupported OAuth attempt database version")
            conn.execute("""CREATE TABLE IF NOT EXISTS oauth_attempts (
                launch_hash TEXT PRIMARY KEY, attempt_id TEXT UNIQUE NOT NULL,
                state_hash TEXT UNIQUE,
                browser_hash TEXT, connector_id TEXT NOT NULL,
                phase TEXT NOT NULL, expires_at REAL NOT NULL, sealed BLOB NOT NULL
            )""")
            conn.execute("PRAGMA user_version=1")

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(self, connector_id, payload, *, now=None):
        now = time.time() if now is None else now
        validate_callback_uri(payload["redirect_uri"])
        launch = secrets.token_urlsafe(32)
        identity = str(uuid.uuid4())
        payload = {
            **payload,
            "connector_id": connector_id,
            "launch_hash": fingerprint(launch),
            "attempt_id": identity,
        }
        sealed = self.vault._cipher().encrypt(json.dumps(payload).encode())
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM oauth_attempts WHERE expires_at<=?", (now,))
            conn.execute(
                "DELETE FROM oauth_attempts WHERE phase IN ('completed','failed') "
                "AND attempt_id NOT IN (SELECT attempt_id FROM oauth_attempts "
                "WHERE phase IN ('completed','failed') ORDER BY expires_at DESC "
                "LIMIT 200)"
            )
            if (
                conn.execute(
                    (
                        "SELECT count(*) FROM oauth_attempts WHERE phase IN "
                        "('pending','issued','consumed')"
                    )
                ).fetchone()[0]
                >= 100
            ):
                raise ValueError("Too many pending authorizations; retry after expiry")
            conn.execute(
                "INSERT INTO oauth_attempts VALUES (?,?,NULL,NULL,?,'pending',?,?)",
                (fingerprint(launch), identity, connector_id, now + 60, sealed),
            )
        return {"ticket": launch, "attempt_id": identity}

    def _payload(self, row):
        try:
            payload = json.loads(self.vault._cipher().decrypt(row["sealed"]))
            if (
                payload["connector_id"] != row["connector_id"]
                or payload["launch_hash"] != row["launch_hash"]
                or payload["attempt_id"] != row["attempt_id"]
            ):
                raise InvalidOAuthAttempt()
            if row["phase"] != "pending" and (
                payload["state_hash"] != row["state_hash"]
                or payload["browser_hash"] != row["browser_hash"]
            ):
                raise InvalidOAuthAttempt()
            return payload
        except Exception:
            raise InvalidOAuthAttempt() from None

    def launch(self, connector_id, ticket, *, now=None):
        now = time.time() if now is None else now
        state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                (
                    "SELECT * FROM oauth_attempts WHERE launch_hash=? AND "
                    "connector_id=? AND phase='pending' AND expires_at>?"
                ),
                (fingerprint(ticket), connector_id, now),
            ).fetchone()
            if row is None:
                raise InvalidOAuthAttempt()
            payload = self._payload(row)
            payload.update(
                state_hash=fingerprint(state), browser_hash=fingerprint(browser)
            )
            sealed = self.vault._cipher().encrypt(json.dumps(payload).encode())
            conn.execute(
                (
                    "UPDATE oauth_attempts SET "
                    "phase='issued',state_hash=?,browser_hash=?,expires_at=?,sealed=?"
                    " WHERE launch_hash=?"
                ),
                (
                    fingerprint(state),
                    fingerprint(browser),
                    now + 600,
                    sealed,
                    row["launch_hash"],
                ),
            )
        return state, browser, payload

    def consume(self, connector_id, state, browser, redirect_uri, *, now=None):
        now = time.time() if now is None else now
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                (
                    "SELECT * FROM oauth_attempts WHERE state_hash=? AND "
                    "browser_hash=? AND connector_id=? AND phase='issued' AND "
                    "expires_at>?"
                ),
                (fingerprint(state), fingerprint(browser), connector_id, now),
            ).fetchone()
            if row is None:
                raise InvalidOAuthAttempt()
            payload = self._payload(row)
            if payload["redirect_uri"] != redirect_uri:
                raise InvalidOAuthAttempt()
            conn.execute(
                "UPDATE oauth_attempts SET phase='consumed' WHERE launch_hash=?",
                (row["launch_hash"],),
            )
        return payload

    def finish(self, payload, *, success):
        with self.connection() as conn:
            conn.execute(
                (
                    "UPDATE oauth_attempts SET phase=? WHERE attempt_id=? AND "
                    "phase='consumed'"
                ),
                ("completed" if success else "failed", payload["attempt_id"]),
            )

    def status(self, connector_id, identity, actor, *, now=None):
        now = time.time() if now is None else now
        with self.connection() as conn:
            row = conn.execute(
                (
                    "SELECT * FROM oauth_attempts WHERE attempt_id=? AND "
                    "connector_id=? AND expires_at>?"
                ),
                (identity, connector_id, now),
            ).fetchone()
        if row is None:
            raise InvalidOAuthAttempt()
        if self._payload(row).get("actor") != actor:
            raise InvalidOAuthAttempt()
        return {"status": row["phase"]}

    def cancel(self, connector_id):
        with self.connection() as conn:
            conn.execute(
                "DELETE FROM oauth_attempts WHERE connector_id=?", (connector_id,)
            )
