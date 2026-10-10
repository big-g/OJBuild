"""Prose formatting must not create unsupported proper-name claims."""

import json

import pytest

from openjarvis.core.evidence import (
    EvidenceRecord,
    GroundingStatus,
    _normalized_text_anchors,
    assess_evidence,
    detect_evidence_requirement,
    validate_response_grounding,
)


class Judge:
    def __init__(self, supported=True):
        self.supported = supported
        self.calls = []

    def generate(self, messages, **kwargs):
        self.calls.append(messages)
        return {
            "content": json.dumps(
                {
                    "supported": self.supported,
                    "unsupported_claims": []
                    if self.supported
                    else ["Unsupported forecast"],
                    "reason": "Checked forecast facts",
                }
            )
        }


def check(answer, judge):
    query = "What is the weather forecast for Kernersville North Carolina?"
    assessment = assess_evidence(
        detect_evidence_requirement(query),
        [
            EvidenceRecord(
                source="test",
                title="Kernersville forecast",
                content="Today is sunny. Tomorrow is sunny. High 72 F. Low 51 F.",
            ),
        ],
    )
    return validate_response_grounding(
        engine=judge,
        model="test",
        query=query,
        answer=answer,
        assessment=assessment,
    )


@pytest.mark.parametrize(
    "heading",
    [
        "Upcoming Days",
        "Coming Days",
        "Next Few Days",
        "Following Week",
        "Previous Several Months",
        "Recent Hours",
        "Past Year",
        "Last Month",
    ],
)
def test_relative_time_prose_reaches_semantic_judge(heading):
    judge = Judge()
    answer = f"**{heading}:**\nToday is sunny. High 72 F."
    result = check(answer, judge)
    assert result.status == GroundingStatus.SUPPORTED
    assert result.method == "llm_judge" and len(judge.calls) == 1
    # The heuristic exemption does not remove anything from semantic validation.
    payload = json.loads(judge.calls[0][1].content)
    assert payload["answer"] == answer


@pytest.mark.parametrize(
    "answer",
    [
        "Upcoming Days\nToday is sunny.",
        "Upcoming Days: Today is sunny.",
        "For the Upcoming Days, today is sunny.",
        "Today is Sunny. Tomorrow is Sunny.",
        "Forecast\nToday is sunny.",
    ],
)
def test_forecast_layout_does_not_join_words_into_names(answer):
    judge = Judge()
    result = check(answer, judge)
    assert result.status == GroundingStatus.SUPPORTED
    assert len(judge.calls) == 1


def test_time_heading_does_not_bypass_semantic_rejection():
    judge = Judge(supported=False)
    result = check("**Upcoming Days:**\nSnow is expected.", judge)
    assert result.status == GroundingStatus.UNSUPPORTED
    assert result.method == "llm_judge" and len(judge.calls) == 1


def test_time_heading_does_not_hide_invented_temperature():
    judge = Judge()
    result = check("**Upcoming Days:**\nHigh 99 F.", judge)
    assert result.status == GroundingStatus.UNSUPPORTED
    assert result.method == "numeric_anchor" and judge.calls == []


def test_real_unsupported_name_remains_blocked():
    judge = Judge()
    result = check("**Upcoming Days:**\nSarah Connor predicts sunny skies.", judge)
    assert result.status == GroundingStatus.UNSUPPORTED
    assert result.method == "name_anchor" and judge.calls == []
    assert "Unsupported name detail: sarah connor" in result.unsupported_claims


def test_real_names_and_hyphens_remain_anchors():
    names = _normalized_text_anchors("Sarah Connor met Jean-Luc Picard.")["name"]
    assert names == {"sarah connor", "jean-luc picard"}
