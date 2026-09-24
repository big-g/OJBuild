"""Evidence integrity primitives.

Evidence is deliberately separate from research citations and tool execution.
A successful tool call does not automatically constitute sufficient evidence.
"""

from __future__ import annotations

import re
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


def tool_supports_evidence(
    tool_spec: Any,
    requirement: EvidenceRequirement,
) -> bool:
    """Whether a ToolSpec explicitly supports this evidence requirement."""
    declared = getattr(tool_spec, "evidence_kinds", None)
    if not isinstance(declared, (list, tuple, set, frozenset)):
        return False

    values = {
        value.value if isinstance(value, EvidenceKind) else str(value).strip()
        for value in declared
    }
    return requirement.kind.value in values


def evidence_records_from_tool_result(
    *,
    tool_name: str,
    metadata: Mapping[str, Any],
    fallback_content: str = "",
) -> list[EvidenceRecord]:
    """Convert the standardized ToolResult evidence contract into records.

    Evidence must be explicitly carried under metadata["evidence"].
    Successful tool output without that contract is not evidence.
    """
    evidence = metadata.get("evidence")
    if not isinstance(evidence, Mapping):
        return []

    provider = str(evidence.get("provider", "") or tool_name).strip()
    retrieved_at = str(evidence.get("retrieved_at", ""))
    provenance_results = evidence.get("records")

    if isinstance(provenance_results, list):
        records: list[EvidenceRecord] = []

        for item in provenance_results:
            if not isinstance(item, Mapping):
                continue

            item_content = str(item.get("content", "")).strip()
            if not item_content:
                continue

            item_metadata = item.get("metadata")
            if not isinstance(item_metadata, Mapping):
                item_metadata = {}

            records.append(
                EvidenceRecord(
                    source=str(item.get("source", "") or provider or tool_name),
                    content=item_content,
                    title=str(item.get("title", "")),
                    url=str(item.get("url", "")),
                    source_id=str(item.get("source_id", "")),
                    retrieved_at=str(item.get("retrieved_at", "") or retrieved_at),
                    metadata={
                        "provider": provider,
                        **dict(item_metadata),
                    },
                )
            )

        return records

    if evidence.get("use_result_content") is not True:
        return []

    content = fallback_content.strip()
    if not content:
        return []

    return [
        EvidenceRecord(
            source=str(evidence.get("source", "") or provider or tool_name),
            content=content,
            title=str(evidence.get("title", "")),
            url=str(evidence.get("url", "")),
            source_id=str(evidence.get("source_id", "")),
            retrieved_at=retrieved_at,
            metadata={
                key: value
                for key, value in evidence.items()
                if key not in {
                    "records",
                    "source",
                    "title",
                    "url",
                    "source_id",
                    "retrieved_at",
                    "use_result_content",
                }
            },
        )
    ]


def evidence_conflict_from_tool_result(metadata: Mapping[str, Any]) -> bool:
    """Return an explicit conflict signal from standardized evidence metadata."""
    evidence = metadata.get("evidence")
    return isinstance(evidence, Mapping) and evidence.get("conflicting") is True


def assess_tool_results(
    requirement: EvidenceRequirement,
    tools: Iterable[Any],
    tool_results: Iterable[Any],
) -> EvidenceAssessment:
    """Assess standardized evidence from tool results against one requirement."""
    if not requirement.required:
        return assess_evidence(requirement)

    tool_specs: dict[str, Any] = {}
    for tool in tools:
        try:
            spec = tool.spec
            name = str(spec.name)
        except Exception:
            continue
        if name:
            tool_specs[name] = spec

    records: list[EvidenceRecord] = []
    conflicting = False

    for tool_result in tool_results:
        if not bool(getattr(tool_result, "success", False)):
            continue

        tool_name = str(getattr(tool_result, "tool_name", ""))
        tool_spec = tool_specs.get(tool_name)
        if tool_spec is None or not tool_supports_evidence(
            tool_spec,
            requirement,
        ):
            continue

        metadata = getattr(tool_result, "metadata", {}) or {}
        if not isinstance(metadata, Mapping):
            continue

        records.extend(
            evidence_records_from_tool_result(
                tool_name=tool_name,
                metadata=metadata,
                fallback_content=str(getattr(tool_result, "content", "")),
            )
        )
        conflicting = (
            conflicting
            or evidence_conflict_from_tool_result(metadata)
        )

    return assess_evidence(
        requirement,
        records,
        conflicting=conflicting,
    )


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
    """Assess one record when trusted provenance is already explicit."""
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


def apply_tool_evidence_to_result(
    requirement: EvidenceRequirement,
    tools: Iterable[Any],
    result: Any,
) -> EvidenceAssessment:
    """Assess tool evidence and apply the final gate to an AgentResult-like object."""
    assessment = assess_tool_results(
        requirement,
        tools,
        getattr(result, "tool_results", ()) or (),
    )

    metadata = getattr(result, "metadata", None)
    if isinstance(metadata, dict):
        metadata.update(
            evidence_result_metadata(
                requirement,
                assessment,
            )
        )

    if assessment.blocked and hasattr(result, "content"):
        result.content = blocked_response(assessment)

    return assessment


def evidence_result_metadata(
    requirement: EvidenceRequirement,
    assessment: EvidenceAssessment,
) -> dict[str, Any]:
    """Return the canonical flat AgentResult evidence metadata."""
    return {
        "evidence_required": bool(requirement.required),
        "evidence_kind": requirement.kind.value,
        "evidence_status": assessment.status.value,
        "evidence_reason": assessment.reason or requirement.reason,
        "evidence_records": len(assessment.records),
    }


def evidence_audit_metadata(
    requirement: EvidenceRequirement,
    assessment: EvidenceAssessment,
) -> dict[str, Any]:
    """Return the normalized trace/audit representation of an assessment."""
    return {
        "required": bool(requirement.required),
        "kind": requirement.kind.value,
        "status": assessment.status.value,
        "reason": assessment.reason or requirement.reason,
        "records": len(assessment.records),
    }


def evidence_audit_from_result_metadata(
    metadata: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Normalize legacy flat AgentResult evidence fields for trace storage."""
    if not metadata.get("evidence_required"):
        return None

    status = str(metadata.get("evidence_status", "")).strip()
    if not status:
        return None

    try:
        records = int(metadata.get("evidence_records", 0) or 0)
    except (TypeError, ValueError):
        records = 0

    return {
        "required": True,
        "kind": str(metadata.get("evidence_kind", "")),
        "status": status,
        "reason": str(metadata.get("evidence_reason", "")),
        "records": max(records, 0),
    }


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
    "currently",
    "right now",
    "today",
    "tonight",
    "tomorrow",
    "yesterday",
    "latest",
    "recent",
    "recently",
    "breaking",
    "open now",
)

_CURRENT_PATTERNS = (
    r"\b(?:what(?:'s| is)|how is)\s+(?:the\s+)?weather\b",
    r"\bweather\s+(?:forecast|in|for|near)\b",
    r"\bforecast\s+(?:for|in|near)\b",
    r"\b(?:what's\s+(?:the\s+)?|what is\s+the\s+|"
    r"how is\s+the\s+)temperature\b",
    r"\btemperature\s+(?:in|at|for|near)\b",
    r"\b(?:current|stock)\s+price\b",
    r"\bprice\s+(?:of|for)\b",
    r"\bhow much (?:is|does)\b",
    r"\bexchange\s+rate\s+(?:for|between|from|to)\b",
    r"\b(?:latest|today(?:'s)?)\s+news\b",
    r"\bnews\s+(?:about|on|from)\b",
    r"\btraffic\s+(?:in|on|near|around)\b",
    r"\b(?:what(?:'s| is)|final)\s+(?:the\s+)?score\b",
    r"\bscore\s+(?:of|for|in)\b",
    r"\b(?:what(?:'s| is)|show me)\s+(?:the\s+)?schedule\b",
    r"\bschedule\s+(?:for|of)\b",
    r"\b(?:is|are)\s+.+?\s+available\b",
    r"\bavailability\s+(?:for|of|at)\b",
    r"\bcurrent\s+(?:weather|temperature|price|exchange\s+rate|news|traffic|"
    r"score|schedule|availability|status|president|prime\s+minister|ceo)\b",
)

_EXTERNAL_TERMS = (
    "look up",
    "search for",
    "search the web",
    "find online",
    "on the internet",
    "according to",
    "what is the latest",
)


_EXTERNAL_PATTERNS = (
    r"\bsearch\s+(?:my|the)\s+(?:email|messages?|files?|documents?|notes?|"
    r"drive|knowledge\s+base|memory)\b",
    r"\bcheck\s+(?:my|the)\s+(?:email|calendar|messages?|files?|documents?|"
    r"notes?|drive)\b",
    r"\bfind\s+.+?\s+in\s+(?:my|the)\s+(?:email|messages?|files?|"
    r"documents?|notes?|drive|knowledge\s+base)\b",
    r"\bhow many\s+(?:emails?|messages?|notes?|documents?|files?|meetings?)\s+"
    r"(?:do i have|did i|have i|are in my|from|with)\b",
    r"\bwho\s+(?:emailed|messaged|contacted)\s+me\b",
    r"\bwhich\s+.+?\s+have i\s+(?:spoken with|met|emailed|messaged|contacted)\b",
)


def detect_evidence_requirement(text: str) -> EvidenceRequirement:
    """Conservatively identify requests requiring externally retrieved data.

    This is intentionally deterministic. The model does not get to decide
    whether evidence is required after it has already generated an answer.
    """
    normalized = " ".join(text.lower().split())

    def contains_term(terms: tuple[str, ...]) -> bool:
        return any(
            re.search(
                rf"(?<!\w){re.escape(term)}(?!\w)",
                normalized,
            )
            is not None
            for term in terms
        )

    current_match = contains_term(_CURRENT_TERMS) or any(
        re.search(pattern, normalized) is not None
        for pattern in _CURRENT_PATTERNS
    )
    external_match = contains_term(_EXTERNAL_TERMS) or any(
        re.search(pattern, normalized) is not None
        for pattern in _EXTERNAL_PATTERNS
    )

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
