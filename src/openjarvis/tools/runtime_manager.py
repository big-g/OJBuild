"""Runtime tools extend ToolTemplate and the existing managed execution gate."""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
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
from openjarvis.tools.runtime_store import (
    VALIDATOR_VERSION,
    RuntimeToolStore,
    validate_definition,
)
from openjarvis.tools.templates.loader import ToolTemplate


class RuntimeTransformTool(ToolTemplate):
    def __init__(self, row, manager):
        definition = validate_definition(json.loads(row["definition"]))
        super().__init__(
            {
                "name": definition["name"],
                "description": definition["description"],
                "parameters": {
                    "type": "object",
                    "properties": {"input": {"type": "string", "maxLength": 32768}},
                    "required": ["input"],
                    "additionalProperties": False,
                },
                "action": {"type": "transform", "transform": definition["transform"]},
            }
        )
        self.management_identity = f"runtime:{row['id']}"
        self.runtime_management_registry = manager.registry
        self._definition_digest = hashlib.sha256(
            json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @property
    def spec(self):
        spec = super().spec
        spec.metadata["runtime_definition_sha256"] = self._definition_digest
        return spec

    def execute(self, **params):
        if set(params) != {"input"} or not isinstance(params["input"], str):
            return ToolResult(
                tool_name=self.tool_id, content="Expected one text input", success=False
            )
        if len(params["input"]) > 32768:
            return ToolResult(
                tool_name=self.tool_id,
                content="Input exceeds 32768 characters",
                success=False,
            )
        # Direct calls also fail closed; normal calls still pass every existing
        # capability, confirmation, taint and trace check in ToolExecutor.
        from openjarvis.security.tool_management_registry import (
            compute_tool_fingerprint,
        )

        registry = self.runtime_management_registry
        record = registry.get(self.management_identity)
        if record is None:
            return ToolResult(
                tool_name=self.tool_id,
                content="Tool is no longer installed",
                success=False,
            )
        fingerprint = compute_tool_fingerprint(
            identity=self.management_identity,
            spec=self.spec,
            provenance=record.provenance,
            implementation_id=record.implementation_id,
        )
        allowed, reason = registry.check_execution(
            self.management_identity, fingerprint=fingerprint
        )
        if not allowed:
            return ToolResult(tool_name=self.tool_id, content=reason, success=False)
        return super().execute(**params)


class RuntimeRegistry(ToolManagementRegistry):
    """Read current SQLite state at execution, including cross-process revocation."""

    def __init__(self, manager):
        super().__init__()
        self.manager = manager

    def get(self, identity):
        if not identity.startswith("runtime:"):
            return None
        try:
            row = self.manager.store.get(identity.removeprefix("runtime:"))
        except KeyError:
            return None
        return self.manager.record(row)


class RuntimeToolManager:
    def __init__(self, db_path):
        self.store = RuntimeToolStore(db_path)
        self.registry = RuntimeRegistry(self)
        self.capabilities = create_builtin_capability_registry()

    def record(self, row):
        tool = RuntimeTransformTool(row, self)
        registry = ToolManagementRegistry()
        record = sync_managed_tool(
            registry,
            tool,
            identity=tool.management_identity,
            provenance=Provenance(
                source_type="runtime",
                source_id=row["id"],
                source_version=VALIDATOR_VERSION,
            ),
            capability_registry=self.capabilities,
            implementation_id=f"{RuntimeTransformTool.__module__}.{RuntimeTransformTool.__qualname__}",
        )
        if (
            row["validator_version"] == VALIDATOR_VERSION
            and row["approved_fingerprint"] == record.fingerprint.value
        ):
            registry.approve(
                record.identity,
                ApprovalRecord(
                    approval_id=f"runtime:{row['id']}:{row['approved_at']}",
                    approved_by=row["approved_by"],
                    timestamp=datetime.fromtimestamp(row["approved_at"], timezone.utc),
                    validation_id=record.validation.validation_id,
                    fingerprint=record.fingerprint,
                ),
            )
        if not row["enabled"] and record.approval is not None:
            record.status = ResourceStatus.DISABLED
        return record

    def view(self, row):
        record = self.record(row)
        return {
            "id": row["id"],
            "revision": row["revision"],
            **json.loads(row["definition"]),
            "enabled": bool(row["enabled"]),
            "approved": bool(record.approval),
            "approved_by": row["approved_by"],
            "status": record.status.value,
            "fingerprint": record.fingerprint.value,
            "required_capabilities": record.spec.required_capabilities,
            "validator_version": VALIDATOR_VERSION,
        }

    def change(self, identity, revision, event, actor, definition=None):
        uuid.UUID(identity)
        row = self.store.get(identity)
        fingerprint = self.record(row).fingerprint.value
        return self.store.change(
            identity,
            revision,
            event,
            actor,
            definition=definition,
            fingerprint=fingerprint,
        )

    def available(self):
        tools = []
        for row in self.store.list():
            if self.record(row).is_approved():
                tools.append(RuntimeTransformTool(row, self))
        return tools

    @contextmanager
    def bind_agent(self, agent):
        """Refresh server-agent tools under its existing per-run model lock."""
        from openjarvis.tools._stubs import ToolExecutor

        executor = getattr(agent, "_executor", None)
        if not isinstance(executor, ToolExecutor) or not hasattr(agent, "_tools"):
            yield
            return
        runtime_tools = self.available()
        if not runtime_tools:
            yield
            return
        old_tools, old_dispatch = agent._tools, executor._tools
        tools = list(old_tools)
        names = {tool.spec.name for tool in tools}
        tools.extend(tool for tool in runtime_tools if tool.spec.name not in names)
        agent._tools = tools
        executor._tools = {tool.spec.name: tool for tool in tools}
        try:
            yield
        finally:
            agent._tools, executor._tools = old_tools, old_dispatch
