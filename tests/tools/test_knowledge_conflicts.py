"""Multi-query knowledge evidence must retain intra-document disagreements."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openjarvis.connectors.store import KnowledgeStore
from openjarvis.core.evidence import (
    ConflictStatus,
    EvidenceRecord,
    apply_tool_evidence_to_result,
    detect_evidence_requirement,
    evidence_records_from_tool_result,
    validate_evidence_conflicts,
)
from openjarvis.tools.knowledge_search import KnowledgeSearchTool


@pytest.fixture()
def store():
    with KnowledgeStore(":memory:") as knowledge:
        yield knowledge


def _search(store, left, right, *, separate_docs=False):
    for index, text in enumerate((left, right)):
        store.store(
            text,
            source="obsidian",
            doc_id=f"policy-{index}" if separate_docs else "policy",
            chunk_index=index,
            title="Migration policy",
            url="https://example.org/policy",
            metadata={
                "section": str(index),
                "version": str(index + 1),
                "jurisdiction": "US",
            },
        )
    tool = KnowledgeSearchTool(store)
    result = tool.execute(query="Migration", queries=["budget", "launch"])
    records = evidence_records_from_tool_result(
        tool_name=result.tool_name, metadata=result.metadata
    )
    assert len(records) == 2
    return tool, result, records


@pytest.mark.parametrize("separate_docs", [False, True])
def test_conflicting_chunks_sharing_document_or_url_are_not_collapsed(
    store, separate_docs
):
    _, _, records = _search(
        store,
        "Migration budget is 100.",
        "Migration budget is 200.",
        separate_docs=separate_docs,
    )
    engine = Mock()
    verdict = validate_evidence_conflicts(
        engine=engine,
        model="test",
        query="What is the Migration budget?",
        records=records,
    )
    assert verdict.status == ConflictStatus.CONFLICTING
    assert verdict.method == "numeric_anchor"
    engine.generate.assert_not_called()


def test_finalizer_blocks_conflicting_knowledge_before_grounding(store):
    tool, search, _ = _search(
        store, "Migration budget is 100.", "Migration budget is 200."
    )
    answer = SimpleNamespace(
        content="Migration budget is 100.", metadata={}, tool_results=[search]
    )
    engine = Mock()
    query = "According to my documents, what is the Migration budget?"
    requirement = detect_evidence_requirement(query)
    assert requirement.required
    assessment = apply_tool_evidence_to_result(
        requirement,
        [tool],
        answer,
        query=query,
        engine=engine,
        model="test",
        validate_conflicts=True,
        validate_grounding=True,
    )
    assert assessment.blocked
    assert answer.content == "The available sources conflict."
    assert answer.metadata["evidence_conflict_status"] == "conflicting"
    engine.generate.assert_not_called()


def test_semantic_validator_sees_distinct_versions_and_verifies_excerpts(store):
    _, _, records = _search(
        store, "Migration launch is approved.", "Migration launch is canceled."
    )
    engine = Mock()
    engine.generate.return_value = {
        "content": json.dumps(
            {
                "conflicting": True,
                "reason": "Launch status differs.",
                "conflicts": [
                    {
                        "claim": "Launch status",
                        "source_ids": ["E1", "E2"],
                        "values": ["approved", "canceled"],
                    }
                ],
            }
        )
    }
    verdict = validate_evidence_conflicts(
        engine=engine,
        model="test",
        query="What is the Migration launch status?",
        records=records,
    )
    assert verdict.conflicting
    payload = json.loads(engine.generate.call_args.args[0][1].content)
    assert len(payload["evidence"]) == 2
    assert {r["metadata"]["version"] for r in payload["evidence"]} == {"1", "2"}
    assert {r["metadata"]["section"] for r in payload["evidence"]} == {"0", "1"}
    assert all(r["metadata"]["jurisdiction"] == "US" for r in payload["evidence"])


def test_version_labels_are_provenance_not_claim_excerpts(store):
    _, _, records = _search(
        store, "Migration launch is approved.", "Migration launch is canceled."
    )
    engine = Mock()
    engine.generate.return_value = {
        "content": json.dumps(
            {
                "conflicting": True,
                "reason": "Versions differ.",
                "conflicts": [
                    {
                        "claim": "Launch status",
                        "source_ids": ["E1", "E2"],
                        "values": ["1", "2"],
                    }
                ],
            }
        )
    }
    verdict = validate_evidence_conflicts(
        engine=engine,
        model="test",
        query="What is the Migration launch status?",
        records=records,
    )
    assert verdict.status == ConflictStatus.VALIDATION_FAILED
    assert verdict.blocked


def test_repeated_chunk_does_not_become_a_second_comparison_unit(store):
    store.store("Migration launch is approved.", doc_id="policy")
    tool = KnowledgeSearchTool(store)
    search = tool.execute(query="Migration", queries=["launch"])
    records = evidence_records_from_tool_result(
        tool_name=search.tool_name, metadata=search.metadata
    )
    engine = Mock()
    verdict = validate_evidence_conflicts(
        engine=engine,
        model="test",
        query="What is the Migration launch status?",
        records=[*records, *records],
    )
    assert verdict.status == ConflictStatus.NOT_CHECKED
    engine.generate.assert_not_called()


def test_complementary_sections_can_pass_semantic_validation(store):
    _, _, records = _search(
        store,
        "Migration launch is approved.",
        "Migration launch is scheduled for October.",
    )
    engine = Mock()
    engine.generate.return_value = {
        "content": json.dumps(
            {
                "conflicting": False,
                "conflicts": [],
                "reason": "Complementary facts.",
            }
        )
    }
    verdict = validate_evidence_conflicts(
        engine=engine,
        model="test",
        query="What is the Migration launch status?",
        records=records,
    )
    assert verdict.status == ConflictStatus.CONSISTENT


def test_comparison_budget_does_not_silently_skip_later_chunks():
    records = [
        EvidenceRecord(
            source="obsidian",
            source_id="policy",
            content=f"Text {i}",
            metadata={"provider": "knowledge_search", "chunk_id": str(i)},
        )
        for i in range(13)
    ]
    engine = Mock()
    verdict = validate_evidence_conflicts(
        engine=engine, model="test", query="Summarize the policy", records=records
    )
    assert verdict.status == ConflictStatus.VALIDATION_FAILED
    assert verdict.method == "payload_budget"
    engine.generate.assert_not_called()


def test_consistent_knowledge_keeps_version_metadata_through_grounding(store):
    tool, search, _ = _search(
        store,
        "Migration launch is approved.",
        "Migration launch is scheduled for October.",
    )
    answer = SimpleNamespace(
        content="Migration launch is approved.", metadata={}, tool_results=[search]
    )
    engine = Mock()
    engine.generate.side_effect = [
        {
            "content": json.dumps(
                {
                    "conflicting": False,
                    "conflicts": [],
                    "reason": "Complementary facts.",
                }
            )
        },
        {
            "content": json.dumps(
                {"supported": True, "unsupported_claims": [], "reason": "Supported."}
            )
        },
    ]
    query = "According to my documents, what is the Migration launch status?"
    assessment = apply_tool_evidence_to_result(
        detect_evidence_requirement(query),
        [tool],
        answer,
        query=query,
        engine=engine,
        model="test",
        validate_conflicts=True,
        validate_grounding=True,
    )
    assert not assessment.blocked
    assert answer.metadata["grounding_status"] == "supported"
    assert engine.generate.call_count == 2
    payload = json.loads(engine.generate.call_args_list[1].args[0][1].content)
    assert {r["metadata"]["version"] for r in payload["evidence"]} == {"1", "2"}
