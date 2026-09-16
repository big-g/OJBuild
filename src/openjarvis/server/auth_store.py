"""Persistent authentication storage for OpenJarvis."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Optional

from openjarvis.core.paths import get_config_dir


_PASSWORD_PREFIX = "scrypt$"
_SESSION_TOKEN_BYTES = 32

# scrypt parameters. These provide deliberately expensive password hashing
# while remaining reasonable for a single-user/home-server deployment.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16


class AuthStore:
    """SQLite-backed storage for users and authentication sessions."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            db_path = get_config_dir() / "auth.db"

        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL DEFAULT '',
                    password_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    disabled INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    last_used_at REAL NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );

                CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_id
                    ON auth_sessions(user_id);

                CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires_at
                    ON auth_sessions(expires_at);
                """
            )

    # ------------------------------------------------------------------
    # Password hashing
    # ------------------------------------------------------------------

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using scrypt with a random salt."""
        if not password:
            raise ValueError("Password cannot be empty")

        salt = os.urandom(_SALT_BYTES)

        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=_SCRYPT_DKLEN,
        )

        return (
            f"{_PASSWORD_PREFIX}"
            f"{_SCRYPT_N}$"
            f"{_SCRYPT_R}$"
            f"{_SCRYPT_P}$"
            f"{salt.hex()}$"
            f"{derived.hex()}"
        )

    @staticmethod
    def verify_password(password: str, encoded: str) -> bool:
        """Verify a password against a stored scrypt hash."""
        try:
            parts = encoded.split("$")

            if len(parts) != 6 or parts[0] != "scrypt":
                return False

            _, n, r, p, salt_hex, hash_hex = parts

            derived = hashlib.scrypt(
                password.encode("utf-8"),
                salt=bytes.fromhex(salt_hex),
                n=int(n),
                r=int(r),
                p=int(p),
                dklen=len(bytes.fromhex(hash_hex)),
            )

            return hmac.compare_digest(
                derived,
                bytes.fromhex(hash_hex),
            )

        except (ValueError, TypeError):
            return False

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def create_user(
        self,
        user_id: str,
        username: str,
        password: str,
        display_name: str = "",
    ) -> None:
        """Create a new authenticated user."""
        if not user_id:
            raise ValueError("user_id cannot be empty")

        if not username:
            raise ValueError("username cannot be empty")

        now = time.time()
        password_hash = self.hash_password(password)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    user_id,
                    username,
                    display_name,
                    password_hash,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    username,
                    display_name,
                    password_hash,
                    now,
                    now,
                ),
            )

    def get_user_by_username(
        self,
        username: str,
    ) -> Optional[sqlite3.Row]:
        """Return a user by username."""
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM users
                WHERE username = ?
                """,
                (username,),
            ).fetchone()

    def get_user(self, user_id: str) -> Optional[sqlite3.Row]:
        """Return a user by user_id."""
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM users
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()

    def authenticate(
        self,
        username: str,
        password: str,
    ) -> Optional[sqlite3.Row]:
        """Authenticate a username/password pair."""
        user = self.get_user_by_username(username)

        if user is None:
            return None

        if user["disabled"]:
            return None

        if not self.verify_password(
            password,
            user["password_hash"],
        ):
            return None

        return user

    def set_password(
        self,
        user_id: str,
        password: str,
    ) -> None:
        """Replace a user's password and revoke existing sessions."""
        if not user_id:
            raise ValueError("user_id cannot be empty")

        password_hash = self.hash_password(password)
        now = time.time()

        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE users
                SET password_hash = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    password_hash,
                    now,
                    user_id,
                ),
            )

            if cursor.rowcount == 0:
                raise ValueError("User not found")

            conn.execute(
                """
                DELETE FROM auth_sessions
                WHERE user_id = ?
                """,
                (user_id,),
            )

    # ------------------------------------------------------------------
    # Session tokens
    # ------------------------------------------------------------------

    @staticmethod
    def _hash_token(token: str) -> str:
        """Hash a session token before storing it."""
        return hashlib.sha256(
            token.encode("utf-8")
        ).hexdigest()

    def create_session(
        self,
        user_id: str,
        expires_in: int = 60 * 60 * 24 * 30,
    ) -> str:
        """Create a random authentication session token."""
        token = secrets.token_urlsafe(_SESSION_TOKEN_BYTES)
        token_hash = self._hash_token(token)

        now = time.time()
        expires_at = now + expires_in

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO auth_sessions (
                    token_hash,
                    user_id,
                    created_at,
                    expires_at,
                    last_used_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    token_hash,
                    user_id,
                    now,
                    expires_at,
                    now,
                ),
            )

        return token

    def get_user_for_token(
        self,
        token: str,
    ) -> Optional[sqlite3.Row]:
        """Return the authenticated user associated with a session token."""
        if not token:
            return None

        token_hash = self._hash_token(token)
        now = time.time()

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT users.*
                FROM auth_sessions
                JOIN users
                    ON users.user_id = auth_sessions.user_id
                WHERE auth_sessions.token_hash = ?
                  AND auth_sessions.expires_at > ?
                  AND users.disabled = 0
                """,
                (token_hash, now),
            ).fetchone()

            if row is not None:
                conn.execute(
                    """
                    UPDATE auth_sessions
                    SET last_used_at = ?
                    WHERE token_hash = ?
                    """,
                    (now, token_hash),
                )

            return row

    def revoke_session(self, token: str) -> None:
        """Revoke an authentication session."""
        if not token:
            return

        token_hash = self._hash_token(token)

        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM auth_sessions
                WHERE token_hash = ?
                """,
                (token_hash,),
            )

    def revoke_all_sessions(self, user_id: str) -> None:
        """Revoke every authentication session belonging to a user."""
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM auth_sessions
                WHERE user_id = ?
                """,
                (user_id,),
            )

    def purge_expired_sessions(self) -> int:
        """Delete expired authentication sessions."""
        now = time.time()

        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM auth_sessions
                WHERE expires_at <= ?
                """,
                (now,),
            )
            return cursor.rowcount
