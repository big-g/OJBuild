"""Durable source schedules and bounded run records, separate from source config."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from openjarvis.connectors.source_store import SourceConflict

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_schedules (
    source_id TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL, enabled INTEGER NOT NULL,
    interval_seconds INTEGER NOT NULL, next_run_at TEXT
);
CREATE TABLE IF NOT EXISTS source_jobs (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    source_revision INTEGER NOT NULL, trigger TEXT NOT NULL,
    state TEXT NOT NULL, phase TEXT NOT NULL, created_at TEXT NOT NULL,
    started_at TEXT, finished_at TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
    documents_seen INTEGER NOT NULL DEFAULT 0, documents_total INTEGER,
    chunks_written INTEGER NOT NULL DEFAULT 0, pages_read INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS source_one_open_job
    ON source_jobs(source_id) WHERE state IN ('queued','running');
CREATE INDEX IF NOT EXISTS source_job_history ON source_jobs(source_id,created_at);
"""


def utc_now():
    return datetime.now(timezone.utc)


class SourceJobs:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _record(row):
        value = dict(row)
        if "cancel_requested" in value:
            value["cancel_requested"] = bool(value["cancel_requested"])
        if "enabled" in value:
            value["enabled"] = bool(value["enabled"])
        return value

    def schedule(self, source_id):
        self.store.get(source_id)
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT * FROM source_schedules WHERE source_id=?", (source_id,)
            ).fetchone()
        return (
            self._record(row)
            if row
            else dict(
                source_id=source_id,
                revision=0,
                enabled=False,
                interval_seconds=3600,
                next_run_at=None,
            )
        )

    def set_schedule(self, source_id, revision, *, enabled, interval_seconds, now=None):
        if (
            not isinstance(enabled, bool)
            or isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, int)
            or not 300 <= interval_seconds <= 604800
        ):
            raise ValueError(
                (
                    "Schedule interval must be 300–604800 seconds and enabled "
                    "must be boolean"
                )
            )
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("Schedule revision must be a nonnegative integer")
        now = now or utc_now()
        next_run = (
            (now + timedelta(seconds=interval_seconds)).isoformat() if enabled else None
        )
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute(
                "SELECT 1 FROM sources WHERE id=?", (source_id,)
            ).fetchone():
                raise KeyError(source_id)
            row = conn.execute(
                "SELECT revision FROM source_schedules WHERE source_id=?", (source_id,)
            ).fetchone()
            current = row["revision"] if row else 0
            if current != revision:
                raise SourceConflict("Schedule changed; refresh before saving")
            conn.execute(
                (
                    "INSERT INTO source_schedules VALUES (?,?,?,?,?) ON "
                    "CONFLICT(source_id) DO UPDATE SET "
                    "revision=excluded.revision,enabled=excluded.enabled,interval"
                    "_seconds=excluded.interval_seconds,next_run_at=excluded.next"
                    "_run_at"
                ),
                (source_id, current + 1, enabled, interval_seconds, next_run),
            )
        return self.schedule(source_id)

    def due(self, now=None):
        with self.store.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    (
                        "SELECT sc.* FROM source_schedules sc JOIN sources s ON "
                        "s.id=sc.source_id WHERE sc.enabled=1 AND s.enabled=1 AND "
                        "sc.next_run_at<=? ORDER BY sc.next_run_at LIMIT 100"
                    ),
                    ((now or utc_now()).isoformat(),),
                )
            ]

    def create(self, record, *, trigger="manual", now=None):
        identity = str(uuid.uuid4())
        now = now or utc_now()
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                (
                    "SELECT 1 FROM source_jobs WHERE source_id=? AND state IN "
                    "('queued','running')"
                ),
                (record["id"],),
            ).fetchone():
                raise SourceConflict("Source is busy; wait for its sync to finish")
            if (
                conn.execute(
                    "SELECT count(*) FROM source_jobs WHERE state='running'"
                ).fetchone()[0]
                >= 2
            ):
                raise SourceConflict("Two sources are syncing; retry when one finishes")
            if trigger == "scheduled":
                schedule = conn.execute(
                    "SELECT * FROM source_schedules WHERE source_id=?", (record["id"],)
                ).fetchone()
                source = conn.execute(
                    "SELECT enabled FROM sources WHERE id=?", (record["id"],)
                ).fetchone()
                if (
                    not schedule
                    or not source
                    or not source["enabled"]
                    or not schedule["enabled"]
                    or schedule["next_run_at"] > now.isoformat()
                ):
                    raise SourceConflict("Schedule is no longer due")
                conn.execute(
                    "UPDATE source_schedules SET next_run_at=? WHERE source_id=?",
                    (
                        (
                            now + timedelta(seconds=schedule["interval_seconds"])
                        ).isoformat(),
                        record["id"],
                    ),
                )
            conn.execute(
                (
                    "INSERT INTO source_jobs "
                    "(id,source_id,source_revision,trigger,state,phase,created_at"
                    ") VALUES (?,?,?,?,'queued','queued',?)"
                ),
                (identity, record["id"], record["revision"], trigger, now.isoformat()),
            )
            conn.execute(
                "UPDATE sources SET state='queued',error=NULL WHERE id=?",
                (record["id"],),
            )
        return self.get(identity)

    def get(self, identity):
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT * FROM source_jobs WHERE id=?", (identity,)
            ).fetchone()
        if row is None:
            raise KeyError(identity)
        return self._record(row)

    def history(self, source_id, limit=20):
        self.store.get(source_id)
        with self.store.connection() as conn:
            return [
                self._record(row)
                for row in conn.execute(
                    (
                        "SELECT * FROM source_jobs WHERE source_id=? ORDER BY "
                        "created_at DESC,id DESC LIMIT ?"
                    ),
                    (source_id, min(max(limit, 1), 100)),
                )
            ]

    def active(self, *, state=None):
        with self.store.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM source_jobs WHERE state=? ORDER BY created_at LIMIT 100"
                if state
                else (
                    "SELECT * FROM source_jobs WHERE state IN "
                    "('queued','running') ORDER BY created_at LIMIT 100"
                ),
                (state,) if state else (),
            )
            return [self._record(row) for row in rows]

    def claim(self, identity):
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if (
                conn.execute(
                    "SELECT count(*) FROM source_jobs WHERE state='running'"
                ).fetchone()[0]
                >= 2
            ):
                raise SourceConflict("Two sources are syncing; retry when one finishes")
            changed = conn.execute(
                (
                    "UPDATE source_jobs SET "
                    "state='running',phase='reading',started_at=? WHERE id=? AND "
                    "state='queued' AND cancel_requested=0"
                ),
                (utc_now().isoformat(), identity),
            ).rowcount
            if not changed:
                raise SourceConflict("Sync job is no longer queued")
            conn.execute(
                (
                    "UPDATE sources SET state='syncing',error=NULL WHERE "
                    "id=(SELECT source_id FROM source_jobs WHERE id=?)"
                ),
                (identity,),
            )

    def progress(self, identity, **values):
        allowed = {
            "phase",
            "documents_seen",
            "documents_total",
            "chunks_written",
            "pages_read",
        }
        if not values or set(values) - allowed:
            raise ValueError("Invalid progress fields")
        with self.store.connection() as conn:
            conn.execute(
                "UPDATE source_jobs SET "
                + ",".join(f"{key}=?" for key in values)
                + " WHERE id=? AND state='running'",
                (*values.values(), identity),
            )

    def begin_commit(self, identity):
        from openjarvis.connectors.sync_control import SyncCancelled

        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT cancel_requested,state FROM source_jobs WHERE id=?", (identity,)
            ).fetchone()
            if not row or row["cancel_requested"] or row["state"] != "running":
                raise SyncCancelled()
            conn.execute(
                "UPDATE source_jobs SET phase='committing' WHERE id=?", (identity,)
            )

    def cancel(self, source_id):
        self.store.get(source_id)
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                (
                    "SELECT * FROM source_jobs WHERE source_id=? AND state IN "
                    "('queued','running')"
                ),
                (source_id,),
            ).fetchone()
            if not row:
                raise SourceConflict("Source has no active sync")
            if row["phase"] == "committing":
                raise SourceConflict("Sync is finishing; cancellation is too late")
            if row["state"] == "queued":
                conn.execute(
                    (
                        "UPDATE source_jobs SET "
                        "state='cancelled',phase='finished',cancel_requested=1,finish"
                        "ed_at=? WHERE id=?"
                    ),
                    (utc_now().isoformat(), row["id"]),
                )
                conn.execute(
                    "UPDATE sources SET state='cancelled',error=NULL WHERE id=?",
                    (source_id,),
                )
            else:
                conn.execute(
                    "UPDATE source_jobs SET cancel_requested=1 WHERE id=?", (row["id"],)
                )
        return self.get(row["id"])

    def finish(self, identity, state, error=None):
        if state not in {"succeeded", "failed", "cancelled", "interrupted"}:
            raise ValueError("Invalid final job state")
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT source_id FROM source_jobs WHERE id=?", (identity,)
            ).fetchone()
            if not row:
                return
            error = str(error)[:1000] if error else None
            conn.execute(
                (
                    "UPDATE source_jobs SET "
                    "state=?,phase='finished',finished_at=?,error=? WHERE id=?"
                ),
                (state, utc_now().isoformat(), error, identity),
            )
            source_state = (
                "idle"
                if state == "succeeded"
                else "cancelled"
                if state == "cancelled"
                else "error"
            )
            conn.execute(
                "UPDATE sources SET state=?,error=? WHERE id=?",
                (source_state, error, row["source_id"]),
            )
            # Keep the most recent 100 terminal runs for this source.
            conn.execute(
                (
                    "DELETE FROM source_jobs WHERE source_id=? AND state NOT IN "
                    "('queued','running') AND id NOT IN (SELECT id FROM "
                    "source_jobs WHERE source_id=? ORDER BY created_at DESC,id "
                    "DESC LIMIT 100)"
                ),
                (row["source_id"], row["source_id"]),
            )
