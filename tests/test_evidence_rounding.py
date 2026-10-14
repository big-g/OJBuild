import json

import pytest

from openjarvis.core.evidence import (
    EvidenceKind,
    EvidenceRecord,
    EvidenceRequirement,
    GroundingStatus,
    assess_evidence,
    validate_response_grounding,
)


class Judge:
    def __init__(self, supported=True):
        self.supported = supported
        self.messages = []

    def generate(self, messages, **kwargs):
        self.messages = messages
        return {
            "content": json.dumps(
                {
                    "supported": self.supported,
                    "unsupported_claims": []
                    if self.supported
                    else ["Wrong entity or time scope"],
                    "reason": "checked",
                }
            )
        }


def validate(source, answer, judge=None):
    judge = judge or Judge()
    assessment = assess_evidence(
        EvidenceRequirement(required=True, kind=EvidenceKind.CURRENT),
        [EvidenceRecord(source="measurements", content=source)],
    )
    result = validate_response_grounding(
        engine=judge,
        model="judge",
        query="Report the latest measurements.",
        answer=answer,
        assessment=assessment,
    )
    return result, judge


def test_decimal_display_rounding_reaches_semantic_validation():
    result, judge = validate(
        '{"value":68.59,"apparent":70.26,"minimum":65.93,"maximum":72.66}',
        "Value 68.6, apparent 70.3, range 65.9 to 72.7.",
    )
    assert result.status == GroundingStatus.SUPPORTED
    assert result.method == "llm_judge"
    assert judge.messages


@pytest.mark.parametrize(
    "source,answer",
    [
        ("Value -1.25", "Value -1.2"),
        ("Value -1.25", "Value -1.3"),
        ("Value 83.96", "Value 84.0"),
        ("Value 0.049", "Value 0.05"),
    ],
)
def test_conventional_rounding_at_the_displayed_precision(source, answer):
    assert validate(source, answer)[0].status == GroundingStatus.SUPPORTED


@pytest.mark.parametrize(
    "source,answer",
    [
        ("Value 68.59", "Value 68.7"),
        ("Value 84.49", "Value 84.0"),
        ("Value 68.59", "Value 68.600"),
        ("Value 68.59", "Count 69"),
        ("Value 83.96", "Value 84.0 and count 84"),
        ("Source https://example.com/68.59", "Value 68.6"),
    ],
)
def test_wrong_values_precision_and_integer_claims_still_block(source, answer):
    result, judge = validate(source, answer)
    assert result.status == GroundingStatus.UNSUPPORTED
    assert result.method == "numeric_anchor"
    assert not judge.messages


def test_rounding_does_not_bypass_semantic_scope_rejection():
    result, judge = validate(
        "Other site current value 68.59.",
        "Requested site future value 68.6.",
        Judge(supported=False),
    )
    assert result.status == GroundingStatus.UNSUPPORTED
    assert result.method == "llm_judge"
    prompt = judge.messages[0].content
    assert "current observation does not establish a future prediction" in prompt
    assert "representative require evidence" in prompt
