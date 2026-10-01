"""Committed configuration events: metadata only, retained after source removal."""

import json
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL, adapter_id TEXT NOT NULL,
    action TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL,
    revision INTEGER NOT NULL, config_version INTEGER NOT NULL,
    previous_version INTEGER, schedule_revision INTEGER,
    changed_fields TEXT NOT NULL, index_reset INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS source_audit_history ON source_audit(source_id,id);
"""


def append_event(
    conn,
    record,
    action,
    *,
    actor="system",
    fields=(),
    index_reset=False,
    previous_version=None,
    schedule_revision=None,
):
    # Never persist settings, URLs, names, request bodies, credential IDs or errors.
    conn.execute(
        "INSERT INTO source_audit (source_id,adapter_id,action,actor,created_at,"
        "revision,config_version,previous_version,schedule_revision,changed_fields,"
        "index_reset) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            record["id"],
            record["adapter_id"],
            action,
            actor,
            datetime.now(timezone.utc).isoformat(),
            record["revision"],
            record["config_version"],
            previous_version,
            schedule_revision,
            json.dumps(sorted(set(fields))),
            bool(index_reset),
        ),
    )


def list_events(store, source_id=None, *, before_id=None, limit=50):
    conditions, params = [], []
    if source_id is not None:
        conditions.append("source_id=?")
        params.append(source_id)
    if before_id is not None:
        conditions.append("id<?")
        params.append(before_id)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM source_audit" + where + " ORDER BY id DESC LIMIT ?",
            (*params, min(max(limit, 1), 100)),
        ).fetchall()
    result = []
    for row in rows:
        value = dict(row)
        value["changed_fields"] = json.loads(value["changed_fields"])
        value["index_reset"] = bool(value["index_reset"])
        result.append(value)
    return result
