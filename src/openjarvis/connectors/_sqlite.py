"""Bounded WAL initialization for databases opened by concurrent sync readers."""

import sqlite3
import time


def initialize_wal(connection):
    # journal_mode changes can report SQLITE_BUSY immediately even when the
    # connection has a busy timeout. Retry only initialization contention; do
    # not retry writes or hide corruption/permission errors.
    timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    deadline = time.monotonic() + 5
    connection.execute("PRAGMA busy_timeout=100")
    try:
        while True:
            try:
                mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                if mode != "wal":
                    mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if mode not in {"wal", "memory"}:
                    raise ValueError("Database could not enable WAL")
                return
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(0.02, remaining))
    finally:
        connection.execute(f"PRAGMA busy_timeout={int(timeout)}")
