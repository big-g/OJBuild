"""Managed tool registration, validation, and approval lifecycle."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from openjarvis.security.capability_registry import (
    ApprovalRecord,
    Provenance,
    ResourceFingerprint,
    ResourceStatus,
    ValidationRecord,
)


@dataclass(slots=True)
class ManagedToolRecord:
    """Governance record for one discovered tool."""

    identity: str
    spec: Any
    provenance: Provenance
    fingerprint: ResourceFingerprint
    implementation_id: str = ""
    is_local: bool = True
    status: ResourceStatus = ResourceStatus.REGISTERED
    validation: ValidationRecord | None = None
    approval: ApprovalRecord | None = None

    @property
    def execution_name(self) -> str:
        return str(getattr(self.spec, "name", ""))

    def is_approved(self) -> bool:
        if self.status != ResourceStatus.APPROVED:
            return False

        if self.validation is None or self.approval is None:
            return False

        if not self.validation.passed:
            return False

        return (
            self.validation.fingerprint == self.fingerprint
            and self.approval.fingerprint == self.fingerprint
            and self.approval.validation_id == self.validation.validation_id
        )


def compute_tool_fingerprint(
    *,
    identity: str,
    spec: Any,
    provenance: Provenance,
    implementation_id: str = "",
    is_local: bool = True,
) -> ResourceFingerprint:
    """Return a deterministic fingerprint of security-relevant tool state."""

    payload = {
        "identity": identity,
        "execution_name": str(getattr(spec, "name", "")),
        "provenance": {
            "source_type": provenance.source_type,
            "source_id": provenance.source_id,
            "source_version": provenance.source_version,
        },
        "implementation_id": implementation_id,
        "is_local": bool(is_local),
        "parameters": getattr(spec, "parameters", {}),
        "category": str(getattr(spec, "category", "") or ""),
        "requires_confirmation": bool(
            getattr(spec, "requires_confirmation", False)
        ),
        "timeout_seconds": float(
            getattr(spec, "timeout_seconds", 30.0)
        ),
        "required_capabilities": sorted(
            str(cap)
            for cap in (
                getattr(spec, "required_capabilities", []) or []
            )
        ),
        "metadata": getattr(spec, "metadata", {}) or {},
    }

    try:
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Tool '{identity}' contains non-canonical fingerprint data"
        ) from exc

    return ResourceFingerprint(
        algorithm="sha256",
        value=hashlib.sha256(canonical).hexdigest(),
    )


class ToolManagementRegistry:
    """Lifecycle/governance registry for discovered tools."""

    def __init__(self) -> None:
        self._records: dict[str, ManagedToolRecord] = {}

    def register(
        self,
        record: ManagedToolRecord,
    ) -> ManagedToolRecord:
        if not record.identity:
            raise ValueError("Tool identity must not be empty")

        if not record.execution_name:
            raise ValueError(
                f"Tool execution name must not be empty: {record.identity}"
            )

        if not record.provenance.source_type:
            raise ValueError(
                f"Tool provenance source_type must not be empty: "
                f"{record.identity}"
            )

        if not record.provenance.source_id:
            raise ValueError(
                f"Tool provenance source_id must not be empty: "
                f"{record.identity}"
            )

        if record.identity in self._records:
            raise ValueError(
                f"Tool already registered: {record.identity}"
            )

        record.status = ResourceStatus.REGISTERED
        record.validation = None
        record.approval = None

        self._records[record.identity] = record
        return record

    def get(
        self,
        identity: str,
    ) -> ManagedToolRecord | None:
        return self._records.get(identity)

    def require(
        self,
        identity: str,
    ) -> ManagedToolRecord:
        record = self.get(identity)

        if record is None:
            raise KeyError(
                f"Unknown managed tool: {identity}"
            )

        return record

    def contains(self, identity: str) -> bool:
        return identity in self._records

    def items(
        self,
    ) -> Iterable[tuple[str, ManagedToolRecord]]:
        return self._records.items()

    def keys(self) -> Iterable[str]:
        return self._records.keys()

    def update_definition(
        self,
        identity: str,
        *,
        spec: Any,
        provenance: Provenance,
        fingerprint: ResourceFingerprint,
        implementation_id: str = "",
        is_local: bool = True,
    ) -> ManagedToolRecord:
        record = self.require(identity)

        changed = fingerprint != record.fingerprint

        record.spec = spec
        record.provenance = provenance
        record.fingerprint = fingerprint
        record.implementation_id = implementation_id
        record.is_local = is_local

        if changed:
            record.status = ResourceStatus.REGISTERED
            record.validation = None
            record.approval = None

        return record

    def validate(
        self,
        identity: str,
        validation: ValidationRecord,
    ) -> ManagedToolRecord:
        record = self.require(identity)

        if validation.fingerprint != record.fingerprint:
            raise ValueError(
                f"Validation fingerprint mismatch: {identity}"
            )

        record.validation = validation
        record.approval = None

        if validation.passed:
            record.status = ResourceStatus.VALIDATED
        else:
            record.status = ResourceStatus.REGISTERED

        return record

    def approve(
        self,
        identity: str,
        approval: ApprovalRecord,
    ) -> ManagedToolRecord:
        record = self.require(identity)

        if record.validation is None:
            raise ValueError(
                f"Tool has not been validated: {identity}"
            )

        if not record.validation.passed:
            raise ValueError(
                f"Tool validation failed: {identity}"
            )

        if record.validation.fingerprint != record.fingerprint:
            raise ValueError(
                f"Tool definition changed since validation: {identity}"
            )

        if approval.validation_id != record.validation.validation_id:
            raise ValueError(
                f"Approval does not reference current validation: "
                f"{identity}"
            )

        if approval.fingerprint != record.fingerprint:
            raise ValueError(
                f"Approval fingerprint mismatch: {identity}"
            )

        record.approval = approval
        record.status = ResourceStatus.APPROVED
        return record

    def disable(
        self,
        identity: str,
    ) -> ManagedToolRecord:
        record = self.require(identity)

        if record.status != ResourceStatus.APPROVED:
            raise ValueError(
                f"Only an approved tool can be disabled: {identity}"
            )

        record.status = ResourceStatus.DISABLED
        return record

    def revoke(
        self,
        identity: str,
    ) -> ManagedToolRecord:
        record = self.require(identity)

        if record.status not in (
            ResourceStatus.APPROVED,
            ResourceStatus.DISABLED,
        ):
            raise ValueError(
                f"Tool is not currently approved: {identity}"
            )

        record.status = ResourceStatus.REVOKED
        record.approval = None
        return record

    def reenable(
        self,
        identity: str,
    ) -> ManagedToolRecord:
        record = self.require(identity)

        if record.status != ResourceStatus.DISABLED:
            raise ValueError(
                f"Only a disabled tool can be re-enabled: {identity}"
            )

        if (
            record.validation is None
            or record.approval is None
            or not record.validation.passed
            or record.validation.fingerprint != record.fingerprint
            or record.approval.fingerprint != record.fingerprint
            or record.approval.validation_id
            != record.validation.validation_id
        ):
            raise ValueError(
                f"Disabled tool no longer has valid approval: {identity}"
            )

        record.status = ResourceStatus.APPROVED
        return record


__all__ = [
    "ManagedToolRecord",
    "ToolManagementRegistry",
    "compute_tool_fingerprint",
]
