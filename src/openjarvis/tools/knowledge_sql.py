"""KnowledgeSQLTool — trusted read-only SQL against the KnowledgeStore.

Allows agents to run SELECT queries for aggregation, counting, ranking,
and filtering operations that BM25 search cannot handle. Queries execute
against a trusted, active-only shadow view so quarantined or tombstoned rows
cannot contribute to answers or evidence.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from openjarvis.connectors.store import KnowledgeStore
from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.memory.store import RECALLABLE_TRUST_TIERS
from openjarvis.tools._stubs import BaseTool, ToolSpec

_MAX_ROWS = 50

_FORBIDDEN_RE = re.compile(
    r"\b(DROP|DELETE|INSERT|UPDATE|ALTER|CREATE|TRUNCATE|ATTACH|DETACH|"
    r"REINDEX|VACUUM|REPLACE)\b",
    re.IGNORECASE,
)

_STRING_LITERAL_RE = re.compile(r"'(?:''|[^'])*'")
_COMMENT_RE = re.compile(r"--|/\*|\*/")
_RELATION_RE = re.compile(r"\b(?:FROM|JOIN)\s+([^\s,;]+)", re.IGNORECASE)
_SCHEMA_BYPASS_RE = re.compile(
    r"(?:^|\W)[\[\x60\"']?\s*(?:main|temp)\s*[\]\x60\"']?\s*\.",
    re.IGNORECASE,
)
_DANGEROUS_FUNCTIONS = frozenset({"load_extension", "readfile", "writefile"})

_SCHEMA_DESCRIPTION = (
    "Table: knowledge_chunks\n"
    "Columns: id, content, source, doc_type, doc_id, title, author, "
    "participants, timestamp, thread_id, url, metadata, chunk_index, "
    "created_at, deleted_at (NULL for active rows)"
)


@ToolRegistry.register("knowledge_sql")
class KnowledgeSQLTool(BaseTool):
    """Run trusted read-only SQL for aggregation over the knowledge store."""

    tool_id = "knowledge_sql"

    def __init__(self, store: Optional[KnowledgeStore] = None) -> None:
        self._store = store

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="knowledge_sql",
            description=(
                "Run a read-only SQL SELECT query against the knowledge_chunks table. "
                "Queries automatically see only active rows whose provenance is "
                "trusted for recall. Use for counting, ranking, aggregation, and "
                f"filtering. {_SCHEMA_DESCRIPTION}"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "SQL SELECT query over knowledge_chunks. Only SELECT "
                            "statements are allowed. Example: SELECT author, "
                            "COUNT(*) as n FROM knowledge_chunks "
                            "WHERE source='imessage' GROUP BY author "
                            "ORDER BY n DESC LIMIT 10"
                        ),
                    },
                },
                "required": ["query"],
            },
            category="knowledge",
            required_capabilities=["memory:read"],
            evidence_kinds=["external"],
        )

    @staticmethod
    def _validate_query(query: str) -> tuple[bool, str, str]:
        """Validate and normalize one SQL statement for the trusted view."""
        query = query.strip()
        if not query:
            return False, "", "No query provided."

        if query.endswith(";"):
            query = query[:-1].rstrip()
        if ";" in query:
            return False, "", "SQL error: only one SELECT statement is allowed."

        normalized = query.lstrip().upper()
        if not normalized.startswith("SELECT"):
            return False, "", "Only SELECT queries are allowed (read-only)."

        scanned = _STRING_LITERAL_RE.sub("''", query)

        if _COMMENT_RE.search(scanned):
            return False, "", "SQL comments are not allowed."

        forbidden = _FORBIDDEN_RE.search(scanned)
        if forbidden:
            return (
                False,
                "",
                (
                    f"Query contains forbidden keyword: "
                    f"{forbidden.group(1).upper()}. Only SELECT queries allowed."
                ),
            )

        if _SCHEMA_BYPASS_RE.search(scanned):
            return (
                False,
                "",
                "Schema-qualified table references are not allowed.",
            )

        relations = _RELATION_RE.findall(scanned)
        reads_knowledge = False
        for raw_relation in relations:
            if raw_relation.startswith("("):
                continue
            relation = raw_relation.strip(chr(96) + '"[]').lower()
            if relation != "knowledge_chunks":
                return (
                    False,
                    "",
                    f"Queries may read only from knowledge_chunks, not {relation}.",
                )
            reads_knowledge = True

        if not reads_knowledge:
            return (
                False,
                "",
                "Queries must read from knowledge_chunks.",
            )

        return True, query, ""

    def _open_query_connection(self) -> sqlite3.Connection:
        """Open an isolated query connection and install the trusted view."""
        if self._store is None:
            raise RuntimeError("No knowledge store configured.")

        db_path = str(getattr(self._store, "_db_path", "") or "")
        if db_path and db_path != ":memory:":
            uri = Path(db_path).resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
        else:
            conn = sqlite3.connect(":memory:")
            self._store._conn.backup(conn)

        conn.row_factory = sqlite3.Row

        trust_literals = ", ".join(
            "'" + tier.replace("'", "''") + "'"
            for tier in sorted(RECALLABLE_TRUST_TIERS)
        )
        conn.execute(
            "CREATE TEMP VIEW knowledge_chunks AS "
            "SELECT * FROM main.knowledge_chunks "
            "WHERE deleted_at IS NULL "
            "AND json_valid(metadata) "
            "AND COALESCE(json_extract(metadata, '$.trust'), '') "
            f"IN ({trust_literals})"
        )
        conn.execute("PRAGMA query_only=ON")

        def authorizer(
            action: int,
            arg1: Optional[str],
            arg2: Optional[str],
            db_name: Optional[str],
            trigger_name: Optional[str],
        ) -> int:
            del db_name, trigger_name
            if action == sqlite3.SQLITE_READ:
                if str(arg1 or "") != "knowledge_chunks":
                    return sqlite3.SQLITE_DENY
            elif action == sqlite3.SQLITE_FUNCTION:
                function_name = str(arg2 or arg1 or "").strip().lower()
                if function_name in _DANGEROUS_FUNCTIONS:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        return conn

    @staticmethod
    def _snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
        """Describe the trusted active input snapshot used by a derivation."""
        row = conn.execute(
            "SELECT COUNT(*) AS n, "
            "MAX(last_synced) AS max_last_synced, "
            "MAX(created_at) AS max_created_at, "
            "COUNT(DISTINCT source) AS source_count "
            "FROM knowledge_chunks"
        ).fetchone()
        sources = conn.execute(
            "SELECT source, COUNT(*) AS n "
            "FROM knowledge_chunks "
            "GROUP BY source ORDER BY n DESC, source ASC LIMIT 20"
        ).fetchall()

        return {
            "trusted_active_rows": int(row["n"] or 0) if row is not None else 0,
            "source_count": int(row["source_count"] or 0) if row is not None else 0,
            "max_last_synced": float(row["max_last_synced"] or 0.0)
            if row is not None
            else 0.0,
            "max_created_at": float(row["max_created_at"] or 0.0)
            if row is not None
            else 0.0,
            "top_sources": [
                {
                    "source": str(source_row["source"] or ""),
                    "rows": int(source_row["n"] or 0),
                }
                for source_row in sources
            ],
        }

    def execute(self, **params: Any) -> ToolResult:
        if self._store is None:
            return ToolResult(
                tool_name="knowledge_sql",
                content="No knowledge store configured.",
                success=False,
            )

        valid, query, error = self._validate_query(
            str(params.get("query", "") or "")
        )
        if not valid:
            return ToolResult(
                tool_name="knowledge_sql",
                content=error,
                success=False,
            )

        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._open_query_connection()
            fetched = conn.execute(query).fetchmany(_MAX_ROWS + 1)
            truncated = len(fetched) > _MAX_ROWS
            rows = fetched[:_MAX_ROWS]
            snapshot = self._snapshot(conn)
        except (sqlite3.Error, RuntimeError, OSError) as exc:
            return ToolResult(
                tool_name="knowledge_sql",
                content=f"SQL error: {exc}",
                success=False,
            )
        finally:
            if conn is not None:
                conn.close()

        retrieved_at = datetime.now(timezone.utc).isoformat()

        if not rows:
            return ToolResult(
                tool_name="knowledge_sql",
                content="Query returned no results.",
                success=True,
                metadata={
                    "num_rows": 0,
                    "truncated": False,
                    "evidence": {
                        "provider": "knowledge_sql",
                        "retrieved_at": retrieved_at,
                        "records": [],
                    },
                },
            )

        columns = list(rows[0].keys())
        lines = [" | ".join(columns)]
        lines.append(" | ".join("---" for _ in columns))
        for row in rows:
            lines.append(" | ".join(str(row[column]) for column in columns))
        output = "\n".join(lines)

        derivation_material = query + "\n" + repr(snapshot) + "\n" + output
        derivation_id = hashlib.sha256(
            derivation_material.encode("utf-8")
        ).hexdigest()[:24]

        return ToolResult(
            tool_name="knowledge_sql",
            content=output,
            success=True,
            metadata={
                "num_rows": len(rows),
                "truncated": truncated,
                "evidence": {
                    "provider": "knowledge_sql",
                    "retrieved_at": retrieved_at,
                    "records": [
                        {
                            "source": "knowledge_store",
                            "source_id": f"derived-sql:{derivation_id}",
                            "content": output,
                            "metadata": {
                                "derived": True,
                                "derivation": "sql",
                                "query": query,
                                "trusted_rows_only": True,
                                "active_rows_only": True,
                                "trust_tiers": sorted(RECALLABLE_TRUST_TIERS),
                                "snapshot": snapshot,
                                "result_rows": len(rows),
                                "truncated": truncated,
                            },
                        }
                    ],
                },
            },
        )


__all__ = ["KnowledgeSQLTool"]
