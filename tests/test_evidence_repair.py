"""Rejected drafts get one revision and must pass the full gate again."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from openjarvis.core.evidence import (
    apply_tool_evidence_to_result,
    detect_evidence_requirement,
    grounding_audit_from_result_metadata,
)
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import ToolSpec

QUERY = "What is the forecast for Kernersville NC?"
CONTENT = "Greensboro NC: Sunday rain chance 70%. Sunday night decreasing clouds."
URL = "https://forecast.example.test/greensboro"


def verdict(supported):
    return {
        "content": json.dumps(
            {
                "supported": supported,
                "unsupported_claims": [] if supported else ["Wrong forecast location"],
                "reason": "Checked location and period",
            }
        )
    }


def run(engine, *, answer="Sunday rain chance 70%.", success=True):
    tool = SimpleNamespace(
        spec=ToolSpec(
            name="forecast",
            description="test",
            evidence_kinds=["current"],
        )
    )
    result = SimpleNamespace(
        content=answer,
        metadata={},
        tool_results=[
            ToolResult(
                tool_name="forecast",
                success=success,
                content=CONTENT,
                metadata={"evidence": {"records": [{"content": CONTENT, "url": URL}]}},
            )
        ],
    )
    apply_tool_evidence_to_result(
        detect_evidence_requirement(QUERY),
        [tool],
        result,
        query=QUERY,
        engine=engine,
        model="test",
        validate_grounding=True,
        repair_grounding=True,
    )
    return result


def test_repair_preserves_actual_source_location_and_revalidates():
    engine = MagicMock()
    candidate = (
        f"The retrieved forecast is for Greensboro NC: Sunday rain chance 70%. {URL}"
    )
    engine.generate.side_effect = [
        verdict(False),
        {"content": candidate},
        verdict(True),
    ]
    result = run(engine)
    assert result.content == candidate
    assert result.metadata["grounding_status"] == "supported"
    assert result.metadata["grounding_initial"]["status"] == "unsupported"
    assert result.metadata["grounding_repair_succeeded"] is True
    audit = grounding_audit_from_result_metadata(result.metadata)
    assert audit["repair_succeeded"] is True
    assert audit["initial"]["status"] == "unsupported"
    assert engine.generate.call_count == 3
    repair_messages = engine.generate.call_args_list[1].args[0]
    payload = json.loads(repair_messages[1].content)
    assert payload["evidence"][0]["content"] == CONTENT
    assert "untrusted data" in repair_messages[0].content
    assert "Never relabel" in repair_messages[0].content
    checked = json.loads(engine.generate.call_args_list[2].args[0][1].content)
    assert checked["answer"] == candidate


def test_second_rejection_stays_blocked_without_another_repair():
    engine = MagicMock()
    engine.generate.side_effect = [
        verdict(False),
        {"content": "Sunday rain chance 70%."},
        verdict(False),
    ]
    result = run(engine)
    assert (
        result.content
        == "I couldn't verify the response against the retrieved evidence."
    )
    assert result.metadata["grounding_repair_succeeded"] is False
    assert engine.generate.call_count == 3


def test_repair_cannot_bypass_numeric_anchor_check():
    engine = MagicMock()
    engine.generate.side_effect = [
        verdict(False),
        {"content": "Sunday rain chance 99%."},
    ]
    result = run(engine)
    assert result.metadata["grounding_method"] == "numeric_anchor"
    assert (
        result.content
        == "I couldn't verify the response against the retrieved evidence."
    )
    assert engine.generate.call_count == 2


@pytest.mark.parametrize(
    "response",
    [
        {"content": ""},
        {"content": "Sunday rain chance 70%.", "tool_calls": [{"name": "forecast"}]},
        {"content": "Sunday rain chance 70%.", "finish_reason": "length"},
        RuntimeError("engine offline"),
    ],
)
def test_failed_or_incomplete_revision_remains_blocked(response):
    engine = MagicMock()
    engine.generate.side_effect = [verdict(False), response]
    result = run(engine)
    assert (
        result.content
        == "I couldn't verify the response against the retrieved evidence."
    )
    assert result.metadata["grounding_repair_succeeded"] is False
    assert engine.generate.call_count == 2


def test_validator_failure_does_not_trigger_revision():
    engine = MagicMock()
    engine.generate.return_value = {"content": "invalid JSON"}
    result = run(engine)
    assert result.metadata["grounding_status"] == "validation_failed"
    assert "grounding_repair_attempted" not in result.metadata
    assert engine.generate.call_count == 1


def test_failed_retrieval_does_not_trigger_revision():
    engine = MagicMock()
    result = run(engine, success=False)
    assert result.content == "I couldn't retrieve the required data."
    engine.generate.assert_not_called()


def test_supported_draft_does_not_trigger_revision():
    engine = MagicMock()
    engine.generate.return_value = verdict(True)
    result = run(engine)
    assert result.content == "Sunday rain chance 70%."
    assert "grounding_repair_attempted" not in result.metadata
    assert engine.generate.call_count == 1
