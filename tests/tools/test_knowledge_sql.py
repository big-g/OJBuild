"""Tests for KnowledgeSQLTool."""

from __future__ import annotations

from pathlib import Path

import pytest

from openjarvis.connectors.store import KnowledgeStore
from openjarvis.core.registry import ToolRegistry


@pytest.fixture()
def store(tmp_path: Path) -> KnowledgeStore:
    ks = KnowledgeStore(str(tmp_path / "test.db"))
    ks.store("Hello from Alice", source="imessage", author="Alice", doc_type="message")
    ks.store(
        "Hello from Alice again", source="imessage", author="Alice", doc_type="message"
    )
    ks.store("Meeting notes Q1", source="granola", author="Bob", doc_type="document")
    ks.store("Email about Spain trip", source="gmail", author="Carol", doc_type="email")
    return ks


def test_spec_declares_external_evidence_only(store: KnowledgeStore) -> None:
    from openjarvis.tools.knowledge_sql import KnowledgeSQLTool

    tool = KnowledgeSQLTool(store=store)
    assert tool.spec.evidence_kinds == ["external"]


def test_select_count(store: KnowledgeStore) -> None:
    from openjarvis.tools.knowledge_sql import KnowledgeSQLTool

    tool = KnowledgeSQLTool(store=store)
    result = tool.execute(query="SELECT COUNT(*) as total FROM knowledge_chunks")
    assert result.success
    assert "4" in result.content


def test_group_by_author(store: KnowledgeStore) -> None:
    from openjarvis.tools.knowledge_sql import KnowledgeSQLTool

    tool = KnowledgeSQLTool(store=store)
    result = tool.execute(
        query=(
            "SELECT author, COUNT(*) as n "
            "FROM knowledge_chunks "
            "GROUP BY author ORDER BY n DESC"
        )
    )
    assert result.success
    assert "Alice" in result.content
    assert "2" in result.content


def test_rejects_non_select(store: KnowledgeStore) -> None:
    from openjarvis.tools.knowledge_sql import KnowledgeSQLTool

    tool = KnowledgeSQLTool(store=store)
    result = tool.execute(query="DELETE FROM knowledge_chunks")
    assert not result.success
    assert "read-only" in result.content.lower() or "SELECT" in result.content


def test_rejects_drop(store: KnowledgeStore) -> None:
    from openjarvis.tools.knowledge_sql import KnowledgeSQLTool

    tool = KnowledgeSQLTool(store=store)
    result = tool.execute(query="DROP TABLE knowledge_chunks")
    assert not result.success


def test_allows_select_with_keyword_substring(store: KnowledgeStore) -> None:
    """A read-only SELECT must not be rejected because a column/alias merely
    contains a write keyword as a substring (e.g. 'created' -> CREATE)."""
    from openjarvis.tools.knowledge_sql import KnowledgeSQLTool

    tool = KnowledgeSQLTool(store=store)
    result = tool.execute(query="SELECT author AS created_author FROM knowledge_chunks")
    assert result.success, result.content
    assert "Alice" in result.content


def test_allows_keyword_inside_string_literal(store: KnowledgeStore) -> None:
    """A write keyword appearing only inside a string literal must not be
    treated as a forbidden statement."""
    from openjarvis.tools.knowledge_sql import KnowledgeSQLTool

    tool = KnowledgeSQLTool(store=store)
    result = tool.execute(
        query="SELECT content FROM knowledge_chunks WHERE content LIKE '%delete%'"
    )
    assert result.success, result.content


def test_rejects_multi_statement(store: KnowledgeStore) -> None:
    """Multi-statement strings fail with a ToolResult, not an exception."""
    from openjarvis.tools.knowledge_sql import KnowledgeSQLTool

    tool = KnowledgeSQLTool(store=store)
    result = tool.execute(query="SELECT 1; VACUUM")
    assert not result.success
    assert "error" in result.content.lower()


def test_handles_bad_sql(store: KnowledgeStore) -> None:
    from openjarvis.tools.knowledge_sql import KnowledgeSQLTool

    tool = KnowledgeSQLTool(store=store)
    result = tool.execute(query="SELECT * FROM nonexistent_table")
    assert not result.success


def test_filter_by_source(store: KnowledgeStore) -> None:
    from openjarvis.tools.knowledge_sql import KnowledgeSQLTool

    tool = KnowledgeSQLTool(store=store)
    result = tool.execute(
        query="SELECT title, author FROM knowledge_chunks WHERE source = 'gmail'"
    )
    assert result.success
    assert "Carol" in result.content


def test_excludes_untrusted_unknown_and_tombstoned_rows(
    store: KnowledgeStore,
) -> None:
    from openjarvis.tools.knowledge_sql import KnowledgeSQLTool

    store.store(
        "Untrusted hidden row",
        source="gmail",
        author="Mallory",
        metadata={"trust": "untrusted"},
    )
    store.store(
        "Unknown-tier hidden row",
        source="gmail",
        author="Future",
        metadata={"trust": "future-tier"},
    )
    tombstoned_id = store.store(
        "Deleted hidden row",
        source="gmail",
        author="Deleted",
        metadata={"trust": "trusted"},
    )
    store._conn.execute(
        "UPDATE knowledge_chunks SET deleted_at = 1 WHERE id = ?",
        (tombstoned_id,),
    )
    store._conn.commit()

    tool = KnowledgeSQLTool(store=store)
    result = tool.execute(
        query="SELECT COUNT(*) as total FROM knowledge_chunks"
    )

    assert result.success, result.content
    assert "4" in result.content
    assert "7" not in result.content
    evidence = result.metadata["evidence"]
    assert evidence["provider"] == "knowledge_sql"
    record = evidence["records"][0]
    assert record["source"] == "knowledge_store"
    assert record["source_id"].startswith("derived-sql:")
    assert record["metadata"]["derived"] is True
    assert record["metadata"]["derivation"] == "sql"
    assert record["metadata"]["trusted_rows_only"] is True
    assert record["metadata"]["active_rows_only"] is True
    assert record["metadata"]["snapshot"]["trusted_active_rows"] == 4


def test_rejects_schema_qualified_trust_bypass(store: KnowledgeStore) -> None:
    from openjarvis.tools.knowledge_sql import KnowledgeSQLTool

    tool = KnowledgeSQLTool(store=store)
    result = tool.execute(
        query="SELECT COUNT(*) FROM main.knowledge_chunks"
    )

    assert not result.success
    assert "schema-qualified" in result.content.lower()


def test_rejects_other_tables_even_for_select(store: KnowledgeStore) -> None:
    from openjarvis.tools.knowledge_sql import KnowledgeSQLTool

    store._conn.execute("CREATE TABLE other_data (value TEXT)")
    store._conn.execute("INSERT INTO other_data VALUES ('secret')")
    store._conn.commit()

    tool = KnowledgeSQLTool(store=store)
    result = tool.execute(query="SELECT value FROM other_data")

    assert not result.success
    assert "only from knowledge_chunks" in result.content.lower()


def test_aggregate_evidence_preserves_sql_and_snapshot(
    store: KnowledgeStore,
) -> None:
    from openjarvis.tools.knowledge_sql import KnowledgeSQLTool

    query = (
        "SELECT author, COUNT(*) as n "
        "FROM knowledge_chunks "
        "GROUP BY author ORDER BY n DESC"
    )
    tool = KnowledgeSQLTool(store=store)
    result = tool.execute(query=query)

    assert result.success, result.content
    evidence = result.metadata["evidence"]
    assert evidence["retrieved_at"]
    assert len(evidence["records"]) == 1
    metadata = evidence["records"][0]["metadata"]
    assert metadata["query"] == query
    assert metadata["result_rows"] >= 1
    assert metadata["snapshot"]["trusted_active_rows"] == 4
    assert set(metadata["trust_tiers"]) == {"", "auto", "trusted"}


def test_derived_sql_satisfies_external_but_not_current_evidence(
    store: KnowledgeStore,
) -> None:
    from openjarvis.core.evidence import (
        EvidenceKind,
        EvidenceRequirement,
        assess_tool_results,
    )
    from openjarvis.tools.knowledge_sql import KnowledgeSQLTool

    tool = KnowledgeSQLTool(store=store)
    result = tool.execute(
        query="SELECT COUNT(*) as total FROM knowledge_chunks"
    )
    external = EvidenceRequirement(
        required=True,
        kind=EvidenceKind.EXTERNAL,
        reason="Personal knowledge retrieval required.",
    )
    current = EvidenceRequirement(
        required=True,
        kind=EvidenceKind.CURRENT,
        reason="Current information required.",
    )

    assert assess_tool_results(external, [tool], [result]).sufficient
    assert assess_tool_results(current, [tool], [result]).blocked


def test_query_without_knowledge_table_is_not_evidence(
    store: KnowledgeStore,
) -> None:
    from openjarvis.tools.knowledge_sql import KnowledgeSQLTool

    tool = KnowledgeSQLTool(store=store)
    result = tool.execute(query="SELECT 1 AS answer")

    assert not result.success
    assert "must read from knowledge_chunks" in result.content.lower()


def test_registered() -> None:
    from openjarvis.tools.knowledge_sql import KnowledgeSQLTool

    ToolRegistry.register_value("knowledge_sql", KnowledgeSQLTool)
    assert ToolRegistry.contains("knowledge_sql")
