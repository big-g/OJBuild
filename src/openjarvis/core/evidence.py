"""Evidence integrity primitives.

Evidence is deliberately separate from research citations and tool execution.
A successful tool call does not automatically constitute sufficient evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Optional


class EvidenceStatus(str, Enum):
    """Terminal assessment of whether required evidence exists."""

    NOT_REQUIRED = "not_required"
    REQUIRED_NOT_OBTAINED = "required_not_obtained"
    OBTAINED = "obtained"
    INSUFFICIENT = "insufficient"
    CONFLICTING = "conflicting"


class EvidenceKind(str, Enum):
    """Why evidence may be required."""

    FACT = "fact"
    CURRENT = "current"
    EXTERNAL = "external"
    DECISION = "decision"


@dataclass(slots=True, frozen=True)
class EvidenceRequirement:
    """Requirement imposed on a response before factual claims are allowed."""

    required: bool
    kind: EvidenceKind
    reason: str = ""


@dataclass(slots=True, frozen=True)
class EvidenceRecord:
    """A concrete piece of externally retrieved evidence."""

    source: str
    content: str
    title: str = ""
    url: str = ""
    source_id: str = ""
    retrieved_at: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """Whether this record contains actual source material."""
        return bool(self.content.strip()) and bool(self.source.strip())

def evidence_records_from_tool_result(
    *,
    tool_name: str,
    metadata: Mapping[str, Any],
    fallback_content: str = "",
) -> list[EvidenceRecord]:
    """Convert structured tool provenance into individual evidence records."""
    provenance_results = metadata.get("results")

    if isinstance(provenance_results, list):
        records: list[EvidenceRecord] = []

        for item in provenance_results:
            if not isinstance(item, Mapping):
                continue

            content = str(item.get("content", "")).strip()
            if not content:
                continue

            records.append(
                EvidenceRecord(
                    source=tool_name,
                    content=content,
                    title=str(item.get("title", "")),
                    url=str(item.get("url", "")),
                    source_id=str(item.get("source_id", "")),
                    metadata={
                        "engine": metadata.get("engine", ""),
                    },
                )
            )

        return records

    content = fallback_content.strip()
    if not content:
        return []

    return [
        EvidenceRecord(
            source=tool_name,
            content=content,
            url=str(metadata.get("url", "")),
            source_id=str(metadata.get("source_id", "")),
            metadata=dict(metadata),
        )
    ]

@dataclass(slots=True, frozen=True)
class EvidenceAssessment:
    """Result of evaluating evidence against a requirement."""

    status: EvidenceStatus
    records: tuple[EvidenceRecord, ...] = ()
    reason: str = ""

    @property
    def sufficient(self) -> bool:
        """Whether the response may rely on this evidence."""
        return self.status in {
            EvidenceStatus.NOT_REQUIRED,
            EvidenceStatus.OBTAINED,
        }

    @property
    def blocked(self) -> bool:
        """Whether evidence integrity prevents a factual response."""
        return not self.sufficient


def assess_evidence(
    requirement: EvidenceRequirement,
    records: Iterable[EvidenceRecord] = (),
    *,
    conflicting: bool = False,
) -> EvidenceAssessment:
    """Assess whether retrieved evidence satisfies a requirement.

    This function is intentionally conservative:
    - no requirement -> allowed
    - required but no usable evidence -> blocked
    - conflicting evidence -> blocked
    - usable evidence -> allowed
    """
    usable = tuple(record for record in records if record.usable)

    if not requirement.required:
        return EvidenceAssessment(
            status=EvidenceStatus.NOT_REQUIRED,
            records=usable,
            reason=requirement.reason,
        )

    if conflicting:
        return EvidenceAssessment(
            status=EvidenceStatus.CONFLICTING,
            records=usable,
            reason="Available sources conflict.",
        )

    if not usable:
        return EvidenceAssessment(
            status=EvidenceStatus.REQUIRED_NOT_OBTAINED,
            reason="Required evidence was not obtained.",
        )

    return EvidenceAssessment(
        status=EvidenceStatus.OBTAINED,
        records=usable,
        reason=requirement.reason,
    )


def assessment_from_tool_result(
    requirement: EvidenceRequirement,
    *,
    success: bool,
    source: str,
    content: str = "",
    title: str = "",
    url: str = "",
    source_id: str = "",
    retrieved_at: str = "",
    metadata: Optional[Mapping[str, Any]] = None,
) -> EvidenceAssessment:
    """Convert one tool result into a conservative evidence assessment.

    Tool success alone is never enough: the returned content must contain
    actual source material.
    """
    if not success:
        return EvidenceAssessment(
            status=EvidenceStatus.REQUIRED_NOT_OBTAINED,
            reason="I couldn't retrieve the required data.",
        )

    record = EvidenceRecord(
        source=source,
        content=content,
        title=title,
        url=url,
        source_id=source_id,
        retrieved_at=retrieved_at,
        metadata=dict(metadata or {}),
    )

    return assess_evidence(requirement, [record])


def blocked_response(assessment: EvidenceAssessment) -> str:
    """Return the canonical user-facing response for a blocked assessment."""
    if assessment.status == EvidenceStatus.CONFLICTING:
        return "The available sources conflict."

    if assessment.status == EvidenceStatus.REQUIRED_NOT_OBTAINED:
        return "I couldn't retrieve the required data."

    if assessment.status == EvidenceStatus.INSUFFICIENT:
        return "Insufficient data to respond."

    return ""


_CURRENT_TERMS = (
    "current",
    "currently",
    "right now",
    "today",
    "tonight",
    "tomorrow",
    "yesterday",
    "latest",
    "recent",
    "recently",
    "forecast",
    "weather",
    "temperature",
    "price",
    "stock price",
    "exchange rate",
    "news",
    "breaking",
    "traffic",
    "score",
    "schedule",
    "availability",
    "open now",
)

_EXTERNAL_TERMS = (
    "look up",
    "search for",
    "search the web",
    "find online",
    "on the internet",
    "online",
    "according to",
    "what does",
    "what is the latest",
)


def detect_evidence_requirement(text: str) -> EvidenceRequirement:
    """Conservatively identify requests requiring externally retrieved data.

    This is intentionally deterministic. The model does not get to decide
    whether evidence is required after it has already generated an answer.
    """
    normalized = " ".join(text.lower().split())

    current_match = any(term in normalized for term in _CURRENT_TERMS)
    external_match = any(term in normalized for term in _EXTERNAL_TERMS)

    if current_match:
        return EvidenceRequirement(
            required=True,
            kind=EvidenceKind.CURRENT,
            reason="The request asks for current or time-sensitive information.",
        )

    if external_match:
        return EvidenceRequirement(
            required=True,
            kind=EvidenceKind.EXTERNAL,
            reason="The request explicitly requires external retrieval.",
        )

    return EvidenceRequirement(
        required=False,
        kind=EvidenceKind.FACT,
    )
