from __future__ import annotations

from datetime import datetime, timezone

from openjarvis.core.types import ToolCall, ToolResult
from openjarvis.security.capability_registry import (
    ApprovalRecord,
    Provenance,
    ValidationCheck,
    ValidationRecord,
)
from openjarvis.security.tool_management_registry import (
    ManagedToolRecord,
    ToolManagementRegistry,
    compute_tool_fingerprint,
)
from openjarvis.tools._stubs import BaseTool, ToolExecutor, ToolSpec


class _ManagedTool(BaseTool):
    tool_id = "managed_exec"

    def __init__(self) -> None:
        self.executed = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.tool_id,
            description="Managed execution probe",
            required_capabilities=["none"],
        )

    def execute(self, **params) -> ToolResult:
        self.executed = True
        return ToolResult(tool_name=self.tool_id, content="executed", success=True)


def _managed_registry(tool: _ManagedTool, *, approve: bool) -> ToolManagementRegistry:
    identity = f"builtin:{tool.spec.name}"
    implementation_id = f"{type(tool).__module__}.{type(tool).__qualname__}"
    provenance = Provenance(source_type="builtin", source_id=implementation_id)
    fingerprint = compute_tool_fingerprint(
        identity=identity,
        spec=tool.spec,
        provenance=provenance,
        implementation_id=implementation_id,
        is_local=tool.is_local,
    )
    record = ManagedToolRecord(
        identity=identity,
        spec=tool.spec,
        provenance=provenance,
        fingerprint=fingerprint,
        implementation_id=implementation_id,
        is_local=tool.is_local,
    )
    registry = ToolManagementRegistry()
    registry.register(record)

    validation = ValidationRecord(
        validation_id="validation",
        validator="test",
        validator_version="1",
        timestamp=datetime.now(timezone.utc),
        fingerprint=fingerprint,
        checks=(ValidationCheck(name="ok", passed=True, severity="error"),),
    )
    registry.validate(identity, validation)

    if approve:
        registry.approve(
            identity,
            ApprovalRecord(
                approval_id="approval",
                approved_by="test",
                timestamp=datetime.now(timezone.utc),
                validation_id=validation.validation_id,
                fingerprint=fingerprint,
            ),
        )
    return registry


def test_validated_but_unapproved_tool_is_blocked():
    tool = _ManagedTool()
    executor = ToolExecutor(
        [tool],
        tool_management_registry=_managed_registry(tool, approve=False),
    )

    result = executor.execute(
        ToolCall(id="blocked", name=tool.tool_id, arguments="{}")
    )

    assert not result.success
    assert "not approved" in result.content
    assert not tool.executed


def test_approved_managed_tool_executes():
    tool = _ManagedTool()
    executor = ToolExecutor(
        [tool],
        tool_management_registry=_managed_registry(tool, approve=True),
    )

    result = executor.execute(
        ToolCall(id="allowed", name=tool.tool_id, arguments="{}")
    )

    assert result.success
    assert tool.executed
