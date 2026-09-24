"""Bootstrap static ToolRegistry entries into managed tool governance."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.security.capabilities import (
    CapabilityResolutionError,
    resolve_declared_tool_capabilities,
)
from openjarvis.security.capability_registry import (
    CapabilityRegistry,
    Provenance,
    ValidationCheck,
    ValidationRecord,
    create_builtin_capability_registry,
)
from openjarvis.security.tool_management_registry import (
    ManagedToolRecord,
    ToolManagementRegistry,
    compute_tool_fingerprint,
)

_VALIDATOR_NAME = "openjarvis.security.tool_management_bootstrap"
_VALIDATOR_VERSION = "1"


def _implementation_id(tool_cls: Any) -> str:
    """Return a stable Python implementation identifier."""
    module = getattr(tool_cls, "__module__", "")
    name = getattr(tool_cls, "__qualname__", getattr(tool_cls, "__name__", ""))

    if not module or not name:
        raise ValueError("Registered tool has no stable Python implementation identity")

    return f"{module}.{name}"


def _validate_builtin_record(
    *,
    registry_key: str,
    tool: Any,
    record: ManagedToolRecord,
    capability_registry: CapabilityRegistry,
) -> ValidationRecord:
    """Validate one built-in tool without authorizing it."""

    checks: list[ValidationCheck] = []

    spec_name = str(getattr(tool.spec, "name", "") or "")

    checks.append(
        ValidationCheck(
            name="registry_key_matches_spec_name",
            passed=registry_key == spec_name,
            severity="error",
            message=(
                ""
                if registry_key == spec_name
                else f"Registry key '{registry_key}' != ToolSpec.name '{spec_name}'"
            ),
        )
    )

    tool_id = getattr(tool, "tool_id", None)
    tool_id_valid = tool_id is None or str(tool_id) == spec_name

    checks.append(
        ValidationCheck(
            name="tool_id_matches_spec_name",
            passed=tool_id_valid,
            severity="error",
            message=(
                ""
                if tool_id_valid
                else f"tool_id '{tool_id}' != ToolSpec.name '{spec_name}'"
            ),
        )
    )

    try:
        resolve_declared_tool_capabilities(tool.spec, capability_registry)
    except CapabilityResolutionError as exc:
        checks.append(
            ValidationCheck(
                name="capability_resolution",
                passed=False,
                severity="error",
                message=str(exc),
            )
        )
    else:
        checks.append(
            ValidationCheck(
                name="capability_resolution",
                passed=True,
                severity="error",
            )
        )

    return ValidationRecord(
        validation_id=uuid.uuid4().hex,
        validator=_VALIDATOR_NAME,
        validator_version=_VALIDATOR_VERSION,
        timestamp=datetime.now(timezone.utc),
        fingerprint=record.fingerprint,
        checks=tuple(checks),
    )


def sync_managed_tool(
    managed: ToolManagementRegistry,
    tool: Any,
    *,
    identity: str,
    provenance: Provenance,
    capability_registry: CapabilityRegistry,
    implementation_id: str = "",
) -> ManagedToolRecord:
    """Register or refresh a runtime-discovered tool without losing approval.

    An unchanged fingerprint preserves existing validation and approval.
    Definition changes reset lifecycle state through update_definition() and
    are revalidated, but never automatically approved.
    """
    spec = tool.spec
    fingerprint = compute_tool_fingerprint(
        identity=identity,
        spec=spec,
        provenance=provenance,
        implementation_id=implementation_id,
        is_local=bool(getattr(tool, "is_local", True)),
    )

    existing = managed.get(identity)
    if existing is not None:
        unchanged = existing.fingerprint == fingerprint
        managed.update_definition(
            identity,
            spec=spec,
            provenance=provenance,
            fingerprint=fingerprint,
            implementation_id=implementation_id,
            is_local=bool(getattr(tool, "is_local", True)),
        )
        if unchanged and existing.validation is not None:
            return existing
        record = existing
    else:
        record = ManagedToolRecord(
            identity=identity,
            spec=spec,
            provenance=provenance,
            fingerprint=fingerprint,
            implementation_id=implementation_id,
            is_local=bool(getattr(tool, "is_local", True)),
        )
        managed.register(record)

    try:
        resolve_declared_tool_capabilities(spec, capability_registry)
    except CapabilityResolutionError as exc:
        checks = (
            ValidationCheck(
                name="capability_resolution",
                passed=False,
                severity="error",
                message=str(exc),
            ),
        )
    else:
        checks = (
            ValidationCheck(
                name="capability_resolution",
                passed=True,
                severity="error",
            ),
        )

    managed.validate(
        identity,
        ValidationRecord(
            validation_id=uuid.uuid4().hex,
            validator=_VALIDATOR_NAME,
            validator_version=_VALIDATOR_VERSION,
            timestamp=datetime.now(timezone.utc),
            fingerprint=fingerprint,
            checks=checks,
        ),
    )
    return record


def build_builtin_tool_management_registry(
    *,
    capability_registry: CapabilityRegistry | None = None,
) -> ToolManagementRegistry:
    """Discover, register, and validate all static ToolRegistry tools.

    Registration and validation never approve execution. Failed validation
    remains visible on the managed record and leaves the tool REGISTERED.
    """

    # Import lazily to avoid making the security registry responsible for
    # static tool module import ordering.
    from openjarvis.agents.tool_resolver import ensure_registries_populated

    ensure_registries_populated()

    capability_registry = (
        capability_registry or create_builtin_capability_registry()
    )
    managed = ToolManagementRegistry()

    for registry_key, tool_cls in sorted(ToolRegistry.items()):
        try:
            tool = tool_cls()
        except Exception as exc:
            raise RuntimeError(
                f"Could not instantiate registered tool '{registry_key}' "
                f"for management: {exc}"
            ) from exc

        spec = tool.spec
        implementation_id = _implementation_id(tool_cls)

        provenance = Provenance(
            source_type="builtin",
            source_id=implementation_id,
        )

        identity = f"builtin:{registry_key}"

        fingerprint = compute_tool_fingerprint(
            identity=identity,
            spec=spec,
            provenance=provenance,
            implementation_id=implementation_id,
            is_local=bool(getattr(tool, "is_local", True)),
        )

        record = ManagedToolRecord(
            identity=identity,
            spec=spec,
            provenance=provenance,
            fingerprint=fingerprint,
            implementation_id=implementation_id,
            is_local=bool(getattr(tool, "is_local", True)),
        )

        managed.register(record)

        validation = _validate_builtin_record(
            registry_key=registry_key,
            tool=tool,
            record=record,
            capability_registry=capability_registry,
        )

        managed.validate(identity, validation)

    return managed


__all__ = [
    "build_builtin_tool_management_registry",
    "sync_managed_tool",
]
