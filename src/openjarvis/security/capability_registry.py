"""Managed capability registration, validation, and approval lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable


class RiskLevel(str, Enum):
    """Advisory risk classification for a capability."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ResourceStatus(str, Enum):
    """Lifecycle state of a managed security resource."""

    REGISTERED = "registered"
    VALIDATED = "validated"
    APPROVED = "approved"
    DISABLED = "disabled"
    REVOKED = "revoked"


@dataclass(slots=True, frozen=True)
class Provenance:
    """Origin of a managed resource."""

    source_type: str
    source_id: str
    source_version: str | None = None


@dataclass(slots=True, frozen=True)
class ResourceFingerprint:
    """Fingerprint of the security-relevant resource identity."""

    algorithm: str
    value: str


@dataclass(slots=True, frozen=True)
class ValidationCheck:
    """One immutable validation result."""

    name: str
    passed: bool
    severity: str
    message: str = ""


@dataclass(slots=True, frozen=True)
class ValidationRecord:
    """Immutable record of a validation operation."""

    validation_id: str
    validator: str
    validator_version: str
    timestamp: datetime
    fingerprint: ResourceFingerprint
    checks: tuple[ValidationCheck, ...]

    @property
    def passed(self) -> bool:
        """Return whether all validation checks passed."""
        return all(check.passed for check in self.checks)


@dataclass(slots=True, frozen=True)
class ApprovalRecord:
    """Immutable administrative approval tied to validation and identity."""

    approval_id: str
    approved_by: str
    timestamp: datetime
    validation_id: str
    fingerprint: ResourceFingerprint


@dataclass(slots=True)
class CapabilityRecord:
    """Managed definition and lifecycle state of a capability."""

    name: str
    description: str
    resource_type: str
    operation: str
    risk_level: RiskLevel
    provenance: Provenance
    status: ResourceStatus = ResourceStatus.REGISTERED
    validation: ValidationRecord | None = None
    approval: ApprovalRecord | None = None

    def is_approved(self) -> bool:
        """Return whether this capability has a currently valid approval."""
        if self.status != ResourceStatus.APPROVED:
            return False

        if self.validation is None or self.approval is None:
            return False

        if not self.validation.passed:
            return False

        return (
            self.approval.validation_id == self.validation.validation_id
            and self.approval.fingerprint == self.validation.fingerprint
        )


# Canonical built-in capability definitions. This is the authoritative
# capability vocabulary; policy and tool metadata consume these definitions.
BUILTIN_CAPABILITIES: tuple[tuple[str, str, str, str, RiskLevel], ...] = (
    (
        "file:read",
        "Read files and other local file-backed resources.",
        "file",
        "read",
        RiskLevel.MEDIUM,
    ),
    (
        "file:write",
        "Create or modify local files.",
        "file",
        "write",
        RiskLevel.HIGH,
    ),
    (
        "network:fetch",
        "Retrieve data from network resources.",
        "network",
        "fetch",
        RiskLevel.MEDIUM,
    ),
    (
        "code:execute",
        "Execute code or commands in a runtime environment.",
        "code",
        "execute",
        RiskLevel.HIGH,
    ),
    (
        "memory:read",
        "Read persistent assistant memory.",
        "memory",
        "read",
        RiskLevel.MEDIUM,
    ),
    (
        "memory:write",
        "Create or modify persistent assistant memory.",
        "memory",
        "write",
        RiskLevel.HIGH,
    ),
    (
        "calendar:read",
        "Allows reading calendar data.",
        "calendar",
        "read",
	RiskLevel.MEDIUM,
    ),
    (
        "channel:read",
        "Allows inspecting messaging-channel availability and connection status.",
        "channel",
        "read",
        RiskLevel.MEDIUM,
    ),
    (
        "channel:send",
        "Send a message through an external communication channel.",
        "channel",
        "send",
        RiskLevel.HIGH,
    ),
    (
        "tool:invoke",
        "Invoke another registered tool.",
        "tool",
        "invoke",
        RiskLevel.HIGH,
    ),
    (
        "schedule:create",
        "Create a scheduled task or automation.",
        "schedule",
        "create",
        RiskLevel.HIGH,
    ),
    (
        "permission:read",
        "Allows reading stored permission/approval decisions.",
        "permission",
        "read",
        RiskLevel.MEDIUM,
    ),
    (
        "system:admin",
        "Perform privileged system or agent administration.",
        "system",
        "admin",
        RiskLevel.CRITICAL,
    ),
)


class CapabilityRegistry:
    """Authoritative registry for known capabilities.

    Registration does not authorize a capability. A capability must be
    validated and explicitly approved before execution policy may rely on it.
    """

    def __init__(self) -> None:
        self._records: dict[str, CapabilityRecord] = {}

    def register(self, record: CapabilityRecord) -> CapabilityRecord:
        """Register a capability definition.

        Existing capability names are rejected rather than silently replaced.
        Capability names are security identities and therefore immutable.
        """
        if not record.name:
            raise ValueError("Capability name must not be empty")

        if record.name in self._records:
            raise ValueError(
                f"Capability already registered: {record.name}"
            )

        self._records[record.name] = record
        return record

    def get(self, name: str) -> CapabilityRecord | None:
        """Return a registered capability, if present."""
        return self._records.get(name)

    def require(self, name: str) -> CapabilityRecord:
        """Return a capability or raise if it is unknown."""
        record = self.get(name)
        if record is None:
            raise KeyError(f"Unknown capability: {name}")
        return record

    def contains(self, name: str) -> bool:
        """Return whether a capability is registered."""
        return name in self._records

    def matches(self, pattern: str) -> tuple[str, ...]:
        """Return known capability names matched by a glob pattern."""
        import fnmatch

        return tuple(name for name in self._records if fnmatch.fnmatch(name, pattern))

    def require_pattern(self, pattern: str) -> tuple[str, ...]:
        """Validate that a capability grant/deny pattern is meaningful."""
        matches = self.matches(pattern)
        if not matches:
            raise KeyError(f"Unknown capability pattern: {pattern}")
        return matches

    def items(self) -> Iterable[tuple[str, CapabilityRecord]]:
        """Return registered capability records."""
        return self._records.items()

    def keys(self) -> Iterable[str]:
        """Return registered capability names."""
        return self._records.keys()

    def validate(
        self,
        name: str,
        validation: ValidationRecord,
    ) -> CapabilityRecord:
        """Attach a validation record to a registered capability."""
        record = self.require(name)

        if not validation.passed:
            record.status = ResourceStatus.REGISTERED
            record.validation = validation
            record.approval = None
            return record

        record.validation = validation
        record.approval = None
        record.status = ResourceStatus.VALIDATED
        return record

    def approve(
        self,
        name: str,
        approval: ApprovalRecord,
    ) -> CapabilityRecord:
        """Approve a capability only against its exact validation."""
        record = self.require(name)

        if record.validation is None:
            raise ValueError(
                f"Capability has not been validated: {name}"
            )

        if not record.validation.passed:
            raise ValueError(
                f"Capability validation failed: {name}"
            )

        if approval.validation_id != record.validation.validation_id:
            raise ValueError(
                f"Approval does not reference current validation: {name}"
            )

        if approval.fingerprint != record.validation.fingerprint:
            raise ValueError(
                f"Approval fingerprint mismatch: {name}"
            )

        record.approval = approval
        record.status = ResourceStatus.APPROVED
        return record

    def disable(self, name: str) -> CapabilityRecord:
        """Disable an approved capability without destroying its history."""
        record = self.require(name)

        if record.status != ResourceStatus.APPROVED:
            raise ValueError(
                f"Only an approved capability can be disabled: {name}"
            )

        record.status = ResourceStatus.DISABLED
        return record

    def revoke(self, name: str) -> CapabilityRecord:
        """Revoke approval; revalidation is required before reapproval."""
        record = self.require(name)

        if record.status not in (
            ResourceStatus.APPROVED,
            ResourceStatus.DISABLED,
        ):
            raise ValueError(
                f"Capability is not currently approved: {name}"
            )

        record.status = ResourceStatus.REVOKED
        record.approval = None
        return record

    def reenable(self, name: str) -> CapabilityRecord:
        """Re-enable a deliberately disabled capability."""
        record = self.require(name)

        if record.status != ResourceStatus.DISABLED:
            raise ValueError(
                f"Only a disabled capability can be re-enabled: {name}"
            )

        if (
            record.validation is None
            or record.approval is None
            or not record.validation.passed
            or record.approval.validation_id != record.validation.validation_id
            or record.approval.fingerprint != record.validation.fingerprint
        ):
            raise ValueError(
                f"Disabled capability no longer has valid approval: {name}"
            )

        record.status = ResourceStatus.APPROVED
        return record


def create_builtin_capability_registry() -> CapabilityRegistry:
    """Create a registry populated with the canonical built-in vocabulary."""
    registry = CapabilityRegistry()
    for name, description, resource_type, operation, risk_level in BUILTIN_CAPABILITIES:
        registry.register(
            CapabilityRecord(
                name=name,
                description=description,
                resource_type=resource_type,
                operation=operation,
                risk_level=risk_level,
                provenance=Provenance(
                    source_type="builtin",
                    source_id="openjarvis.security.capability_registry",
                ),
            )
        )
    return registry


__all__ = [
    "ApprovalRecord",
    "BUILTIN_CAPABILITIES",
    "CapabilityRecord",
    "CapabilityRegistry",
    "Provenance",
    "ResourceFingerprint",
    "ResourceStatus",
    "RiskLevel",
    "ValidationCheck",
    "ValidationRecord",
    "create_builtin_capability_registry",
]
