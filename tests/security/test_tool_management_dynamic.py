from __future__ import annotations

from datetime import datetime, timezone

from openjarvis.core.types import ToolResult
from openjarvis.security.capability_registry import (
    ApprovalRecord,
    Provenance,
    ResourceStatus,
    create_builtin_capability_registry,
)
from openjarvis.security.tool_management_bootstrap import sync_managed_tool
from openjarvis.security.tool_management_registry import ToolManagementRegistry
from openjarvis.tools._stubs import BaseTool, ToolSpec


class _DynamicManagedTool(BaseTool):
    tool_id = "dynamic_managed"

    def __init__(self, *, changed: bool = False) -> None:
        self._changed = changed

    @property
    def spec(self) -> ToolSpec:
        properties = {"value": {"type": "string"}}
        if self._changed:
            properties["extra"] = {"type": "boolean"}
        return ToolSpec(
            name=self.tool_id,
            description="Dynamic management probe",
            parameters={"type": "object", "properties": properties},
            required_capabilities=["file:read"],
        )

    def execute(self, **params) -> ToolResult:
        return ToolResult(tool_name=self.tool_id, content="ok", success=True)


def _sync(registry: ToolManagementRegistry, tool: _DynamicManagedTool):
    return sync_managed_tool(
        registry,
        tool,
        identity="template:dynamic_managed",
        provenance=Provenance(source_type="template", source_id="test-template"),
        capability_registry=create_builtin_capability_registry(),
        implementation_id=f"{type(tool).__module__}.{type(tool).__qualname__}",
    )


def test_unchanged_dynamic_rediscovery_preserves_approval():
    managed = ToolManagementRegistry()
    record = _sync(managed, _DynamicManagedTool())

    validation = record.validation
    assert validation is not None
    managed.approve(
        record.identity,
        ApprovalRecord(
            approval_id="approval",
            approved_by="test",
            timestamp=datetime.now(timezone.utc),
            validation_id=validation.validation_id,
            fingerprint=record.fingerprint,
        ),
    )
    assert record.status == ResourceStatus.APPROVED

    same = _sync(managed, _DynamicManagedTool())

    assert same.status == ResourceStatus.APPROVED
    assert same.approval is not None


def test_changed_dynamic_rediscovery_invalidates_approval():
    managed = ToolManagementRegistry()
    record = _sync(managed, _DynamicManagedTool())

    validation = record.validation
    assert validation is not None
    managed.approve(
        record.identity,
        ApprovalRecord(
            approval_id="approval",
            approved_by="test",
            timestamp=datetime.now(timezone.utc),
            validation_id=validation.validation_id,
            fingerprint=record.fingerprint,
        ),
    )

    changed = _sync(managed, _DynamicManagedTool(changed=True))

    assert changed.status == ResourceStatus.VALIDATED
    assert changed.validation is not None
    assert changed.validation.passed
    assert changed.approval is None
