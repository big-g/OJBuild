#!/usr/bin/env python3
"""Read current MCP settings and recent retrieval traces without making tool calls."""

import argparse
import json
import sqlite3
from pathlib import Path


def decoded(value):
    if not isinstance(value, str) or len(value) > 262144:
        return {}
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except ValueError:
        return {}


def connect(path):
    return sqlite3.connect(
        Path(path).expanduser().resolve().as_uri() + "?mode=ro", uri=True
    )


def diagnose(trace_path, mcp_path, contains="forecast", limit=3):
    from openjarvis.mcp.runtime_store import RuntimeMCPStore

    report = {"mcp_connections": [], "attempts": []}
    if Path(mcp_path).expanduser().is_file():
        db = connect(mcp_path)
        try:
            db.row_factory = sqlite3.Row
            for row in db.execute("SELECT * FROM runtime_mcp ORDER BY name"):
                settings = decoded(row["definition"])
                report["mcp_connections"].append(
                    {
                        "name": row["name"],
                        "enabled": bool(row["enabled"]),
                        "approved": RuntimeMCPStore.approved(dict(row)),
                        "automatic_calls": settings.get(
                            "allow_without_confirmation", False
                        ),
                        "evidence_tools": settings.get("evidence_tools", {}),
                    }
                )
        finally:
            db.close()
    else:
        report["mcp_database"] = "missing"
    if not Path(trace_path).expanduser().is_file():
        report["trace_database"] = "missing"
        return report
    db = connect(trace_path)
    try:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT trace_id, model, agent, metadata FROM traces "
            "WHERE query LIKE ? ORDER BY started_at DESC LIMIT ?",
            ("%" + contains + "%", min(5, max(1, limit))),
        ).fetchall()
        for row in rows:
            attempt = {
                "trace_id": row["trace_id"],
                "model": row["model"],
                "agent": row["agent"],
                "evidence": decoded(row["metadata"]).get("evidence", {}),
                "tool_calls": [],
            }
            for step in db.execute(
                "SELECT input, output, metadata FROM trace_steps "
                "WHERE trace_id=? AND step_type='tool_call' "
                "ORDER BY step_index LIMIT 20",
                (row["trace_id"],),
            ):
                output = decoded(step["output"])
                metadata = decoded(step["metadata"])
                attempt["tool_calls"].append(
                    {
                        "tool": decoded(step["input"]).get("tool", ""),
                        "success": output.get("success"),
                        "has_evidence": bool(metadata.get("evidence")),
                    }
                )
            report["attempts"].append(attempt)
    finally:
        db.close()
    return report


def main():
    from openjarvis.core.config import load_config

    config = load_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contains", default="forecast", help="Query text to match")
    parser.add_argument("--limit", type=int, choices=range(1, 6), default=3)
    args = parser.parse_args()
    runtime = Path(config.security.runtime_tools_db_path).expanduser()
    report = diagnose(
        config.traces.db_path,
        runtime.with_name(runtime.stem + "_mcp.db"),
        args.contains,
        args.limit,
    )
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
