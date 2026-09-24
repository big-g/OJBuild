import pytest

from openjarvis.core.evidence import (
    EvidenceKind,
    EvidenceRecord,
    EvidenceRequirement,
    EvidenceStatus,
    assess_evidence,
    assessment_from_tool_result,
    blocked_response,
    tool_supports_evidence,
)

from openjarvis.core.evidence import evidence_records_from_tool_result

def required_current():
    return EvidenceRequirement(
        required=True,
        kind=EvidenceKind.CURRENT,
        reason="Current information requires retrieval.",
    )


def test_required_evidence_without_records_is_blocked():
    assessment = assess_evidence(required_current())

    assert assessment.status == EvidenceStatus.REQUIRED_NOT_OBTAINED
    assert assessment.blocked
    assert blocked_response(assessment) == "I couldn't retrieve the required data."


def test_successful_tool_with_empty_content_is_not_evidence():
    assessment = assessment_from_tool_result(
        required_current(),
        success=True,
        source="web_search",
        content="",
    )

    assert assessment.status == EvidenceStatus.REQUIRED_NOT_OBTAINED
    assert assessment.blocked


def test_successful_tool_with_real_content_produces_evidence():
    assessment = assessment_from_tool_result(
        required_current(),
        success=True,
        source="weather_api",
        content="Tomorrow: high 82°F, low 68°F.",
        title="Weather forecast",
        url="https://example.test/weather",
        source_id="forecast-123",
    )

    assert assessment.status == EvidenceStatus.OBTAINED
    assert assessment.sufficient
    assert len(assessment.records) == 1
    assert assessment.records[0].source == "weather_api"


def test_failed_tool_is_blocked():
    assessment = assessment_from_tool_result(
        required_current(),
        success=False,
        source="weather_api",
        content="Tomorrow: 82°F.",
    )

    assert assessment.status == EvidenceStatus.REQUIRED_NOT_OBTAINED
    assert assessment.blocked
    assert blocked_response(assessment) == "I couldn't retrieve the required data."


def test_conflicting_sources_are_blocked():
    records = [
        EvidenceRecord(
            source="source_a",
            content="Tomorrow: 82°F.",
        ),
        EvidenceRecord(
            source="source_b",
            content="Tomorrow: 91°F.",
        ),
    ]

    assessment = assess_evidence(
        required_current(),
        records,
        conflicting=True,
    )

    assert assessment.status == EvidenceStatus.CONFLICTING
    assert assessment.blocked
    assert blocked_response(assessment) == "The available sources conflict."


def test_non_required_evidence_does_not_block():
    requirement = EvidenceRequirement(
        required=False,
        kind=EvidenceKind.FACT,
    )

    assessment = assess_evidence(requirement)

    assert assessment.status == EvidenceStatus.NOT_REQUIRED
    assert assessment.sufficient


def test_weather_forecast_requires_current_evidence():
    from openjarvis.core.evidence import detect_evidence_requirement

    requirement = detect_evidence_requirement(
        "What's the weather forecast for the next two days?"
    )

    assert requirement.required
    assert requirement.kind == EvidenceKind.CURRENT


def test_latest_information_requires_evidence():
    from openjarvis.core.evidence import detect_evidence_requirement

    requirement = detect_evidence_requirement(
        "What is the latest information about this?"
    )

    assert requirement.required


def test_normal_reasoning_does_not_require_external_evidence():
    from openjarvis.core.evidence import detect_evidence_requirement

    requirement = detect_evidence_requirement(
        "Explain how a diesel engine works."
    )

    assert not requirement.required

def test_evidence_records_from_tool_result_preserves_each_provenance_source():
    records = evidence_records_from_tool_result(
        tool_name="web_search",
        metadata={
            "evidence": {
                "provider": "duckduckgo",
                "retrieved_at": "2026-09-24T10:00:00+00:00",
                "records": [
                    {
                        "title": "Source A",
                        "url": "https://example.test/a",
                        "content": "Content A",
                    },
                    {
                        "title": "Source B",
                        "url": "https://example.test/b",
                        "content": "Content B",
                    },
                ],
            }
        },
    )

    assert len(records) == 2

    assert records[0].title == "Source A"
    assert records[0].url == "https://example.test/a"
    assert records[0].content == "Content A"

    assert records[1].title == "Source B"
    assert records[1].url == "https://example.test/b"
    assert records[1].content == "Content B"


def test_evidence_records_from_tool_result_ignores_empty_provenance():
    records = evidence_records_from_tool_result(
        tool_name="web_search",
        metadata={
            "evidence": {
                "provider": "duckduckgo",
                "records": [
                    {
                        "title": "Empty",
                        "url": "https://example.test/empty",
                        "content": "",
                    },
                    {
                        "title": "Valid",
                        "url": "https://example.test/valid",
                        "content": "Valid content.",
                    },
                ],
            }
        },
    )

    assert len(records) == 1
    assert records[0].title == "Valid"



def test_unmarked_tool_metadata_is_not_evidence():
    records = evidence_records_from_tool_result(
        tool_name="web_search",
        metadata={
            "results": [
                {
                    "title": "Legacy-looking result",
                    "url": "https://example.test/result",
                    "content": "Content that must not bypass the evidence contract.",
                }
            ]
        },
        fallback_content="Also must not count.",
    )

    assert records == []


def test_tool_spec_must_declare_matching_evidence_kind():
    class _Spec:
        evidence_kinds = ["external"]

    assert not tool_supports_evidence(_Spec(), required_current())

    requirement = EvidenceRequirement(
        required=True,
        kind=EvidenceKind.EXTERNAL,
        reason="External retrieval required.",
    )
    assert tool_supports_evidence(_Spec(), requirement)



def test_what_does_question_does_not_force_external_retrieval():
    from openjarvis.core.evidence import detect_evidence_requirement

    requirement = detect_evidence_requirement(
        "What does WAL mode mean?"
    )

    assert not requirement.required


def test_online_as_technical_adjective_does_not_force_external_retrieval():
    from openjarvis.core.evidence import detect_evidence_requirement

    requirement = detect_evidence_requirement(
        "What are online algorithms?"
    )

    assert not requirement.required



@pytest.mark.parametrize(
    "query",
    [
        "What is electrical current?",
        "Explain price elasticity.",
        "What does high availability mean?",
        "What is temperature?",
    ],
)
def test_static_concepts_do_not_require_current_evidence(query):
    from openjarvis.core.evidence import detect_evidence_requirement

    assert not detect_evidence_requirement(query).required


@pytest.mark.parametrize(
    "query",
    [
        "Who is the current president?",
        "What's the price of Bitcoin?",
        "What's the weather in Charlotte?",
        "What is the temperature?",
        "What's the temperature?",
        "What is the exchange rate from USD to GBP?",
    ],
)
def test_dynamic_queries_require_current_evidence(query):
    from openjarvis.core.evidence import detect_evidence_requirement

    requirement = detect_evidence_requirement(query)

    assert requirement.required
    assert requirement.kind == EvidenceKind.CURRENT
