"""Acceptance tests for bounded, evidence-preserving multi-query retrieval."""

import json
from unittest.mock import MagicMock, Mock

import pytest

from openjarvis.agents.deep_research import DeepResearchAgent
from openjarvis.connectors.retriever import TwoStageRetriever
from openjarvis.connectors.store import KnowledgeStore
from openjarvis.core.evidence import evidence_records_from_tool_result
from openjarvis.tools.knowledge_search import KnowledgeSearchTool
from openjarvis.tools.storage._stubs import RetrievalResult


@pytest.fixture()
def store():
    with KnowledgeStore(":memory:") as knowledge:
        yield knowledge


@pytest.mark.parametrize("rerank", [False, True])
def test_multi_query_merges_chunks_with_filters_and_provenance(store, rerank):
    shared = store.store(
        "Alpha approval and Beta budget",
        source="obsidian",
        doc_id="plan",
        url="https://example.org/plan",
        metadata={"version": "2", "section": "A"},
    )
    unique = store.store(
        "Beta cost",
        source="obsidian",
        doc_id="cost",
        metadata={"jurisdiction": "US"},
    )
    store.store("Alpha Beta excluded", source="gmail")
    store.store("Alpha Beta quarantined", metadata={"trust": "untrusted"})
    tool = KnowledgeSearchTool(
        store=store,
        retriever=TwoStageRetriever(store) if rerank else None,
    )

    result = tool.execute(query="Alpha", queries=["Beta", "Alpha"], source="obsidian")

    assert result.success
    assert result.metadata["queries"] == ["Alpha", "Beta"]
    records = result.metadata["evidence"]["records"]
    assert len(records) == 2
    assert {r["metadata"]["chunk_id"] for r in records} == {shared, unique}
    assert records[0]["source_id"] == "plan"
    assert records[0]["url"] == "https://example.org/plan"
    assert records[0]["metadata"]["matched_queries"] == ["Alpha", "Beta"]
    assert records[0]["metadata"]["version"] == "2"
    assert records[0]["metadata"]["section"] == "A"
    assert records[1]["metadata"]["jurisdiction"] == "US"
    assert "version: 2" in result.content and "section: A" in result.content
    assert "jurisdiction: US" in result.content
    evidence = evidence_records_from_tool_result(
        tool_name=result.tool_name,
        metadata=result.metadata,
    )
    assert len(evidence) == 2
    assert evidence[0].metadata["version"] == "2"
    assert "excluded" not in result.content
    assert "quarantined" not in result.content


def test_distinct_sections_and_conflicting_versions_remain_evidence(store):
    for index, (content, version) in enumerate(
        [
            ("Alpha deadline is October 1", "1"),
            ("Beta deadline is October 2", "2"),
        ]
    ):
        store.store(
            content,
            doc_id="policy",
            chunk_index=index,
            metadata={"version": version, "section": str(index)},
        )
    result = KnowledgeSearchTool(store).execute(query="Alpha", queries=["Beta"])
    records = result.metadata["evidence"]["records"]
    assert len(records) == 2
    assert {r["metadata"]["version"] for r in records} == {"1", "2"}
    assert "October 1" in result.content and "October 2" in result.content


def test_rank_fusion_is_independent_of_backend_score_scales():
    a = RetrievalResult("A", score=99999, metadata={"chunk_id": "a"})
    b = RetrievalResult("B", score=0.01, metadata={"chunk_id": "b"})
    backend = Mock()
    backend.retrieve.side_effect = [[a, b], [b]]
    result = KnowledgeSearchTool(retriever=backend).execute(
        query="one",
        queries=["two"],
        top_k=1,
    )
    assert result.metadata["num_results"] == 1
    assert result.metadata["evidence"]["records"][0]["content"] == "B"
    # Preserve the native score rather than presenting fused rank as evidence.
    assert result.metadata["evidence"]["records"][0]["metadata"]["score"] == 0.01


def test_filters_apply_to_every_query_and_duplicate_queries_run_once():
    backend = Mock()
    backend.retrieve.return_value = []
    result = KnowledgeSearchTool(retriever=backend).execute(
        query=" one ",
        queries=["two", "one"],
        top_k=3,
        source="gmail",
        doc_type="email",
        author="Gary",
        since="2026-01-01",
        until="2026-12-31",
    )
    assert result.success
    assert backend.retrieve.call_count == 2
    for call in backend.retrieve.call_args_list:
        assert call.kwargs == dict(
            top_k=3,
            source="gmail",
            doc_type="email",
            author="Gary",
            since="2026-01-01",
            until="2026-12-31",
        )


def test_no_matching_subquery_keeps_other_evidence(store):
    store.store("Beta budget", doc_id="budget")
    result = KnowledgeSearchTool(store).execute(query="absent", queries=["Beta"])
    assert result.success and result.metadata["num_results"] == 1


def test_failed_subquery_does_not_return_partial_success():
    backend = Mock()
    backend.retrieve.side_effect = [
        [RetrievalResult("Partial")],
        RuntimeError("offline"),
    ]
    result = KnowledgeSearchTool(retriever=backend).execute(
        query="one", queries=["two"]
    )
    assert not result.success
    assert result.metadata["failed_query"] == "two"
    assert "evidence" not in result.metadata
    assert "Partial" not in result.content


def test_untrusted_backend_results_are_filtered_before_fusion():
    backend = Mock()
    backend.retrieve.return_value = [
        RetrievalResult("Quarantined", metadata={"trust": "untrusted"}),
        RetrievalResult("Allowed", metadata={"trust": "trusted"}),
    ]
    result = KnowledgeSearchTool(retriever=backend).execute(
        query="one", queries=["two"]
    )
    assert result.metadata["num_results"] == 1
    assert "Quarantined" not in result.content


def test_same_text_in_independent_sources_is_not_deduplicated():
    backend = Mock()
    backend.retrieve.side_effect = [
        [RetrievalResult("same", source="gmail", metadata={"doc_id": "one"})],
        [RetrievalResult("same", source="slack", metadata={"doc_id": "two"})],
    ]
    result = KnowledgeSearchTool(retriever=backend).execute(
        query="one", queries=["two"]
    )
    assert result.metadata["num_results"] == 2


@pytest.mark.parametrize(
    "params",
    [
        {"queries": "two"},
        {"queries": [" "]},
        {"queries": [None]},
        {"queries": ["a"] * 5},
        {"queries": ["a" * 2001]},
        {"query": "a" * 2001},
        {"query": " "},
        {"query": None},
        {"top_k": 0},
        {"top_k": 51},
        {"top_k": 1.5},
        {"top_k": True},
        {"top_k": "invalid"},
    ],
)
def test_invalid_parameters_fail_before_search(params):
    backend = Mock()
    arguments = {"query": "one", **params}
    result = KnowledgeSearchTool(retriever=backend).execute(**arguments)
    assert not result.success
    backend.retrieve.assert_not_called()


def test_tool_schema_advertises_bounds():
    properties = KnowledgeSearchTool().spec.parameters["properties"]
    assert properties["queries"]["maxItems"] == 4
    assert properties["top_k"]["maximum"] == 50


def test_research_agent_invokes_focused_queries_and_receives_merged_evidence(store):
    store.store("Alpha approval", source="gmail", title="Approval")
    store.store("Beta budget", source="obsidian", title="Budget")
    engine = MagicMock()
    engine.engine_id = "mock"
    engine.generate.side_effect = [
        {
            "content": "",
            "usage": {},
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "id": "multi",
                    "type": "function",
                    "function": {
                        "name": "knowledge_search",
                        "arguments": json.dumps(
                            {"query": "Alpha", "queries": ["Beta"]}
                        ),
                    },
                }
            ],
        },
        {
            "content": "Alpha approval and Beta budget.",
            "usage": {},
            "finish_reason": "stop",
        },
    ]
    agent = DeepResearchAgent(engine, "test", tools=[KnowledgeSearchTool(store)])
    answer = agent.run("Find Alpha approval and Beta budget")
    searches = [r for r in answer.tool_results if r.tool_name == "knowledge_search"]
    assert len(searches) == 1 and searches[0].success
    assert searches[0].metadata["queries"] == ["Alpha", "Beta"]
    assert searches[0].metadata["num_results"] == 2
    assert {r["source"] for r in searches[0].metadata["evidence"]["records"]} == {
        "gmail",
        "obsidian",
    }
