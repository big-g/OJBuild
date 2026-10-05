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
from openjarvis.tools.runtime_adapters import (
    adapter_config,
    evaluate_formula,
    get_adapter,
)
from openjarvis.tools.runtime_store import (
    RuntimeToolStore,
    validate_definition,
)
from openjarvis.tools.templates.loader import ToolTemplate


class RuntimeTransformTool(ToolTemplate):
    def __init__(self, row, manager):
        definition = validate_definition(json.loads(row["definition"]))
        self._adapter = get_adapter(definition)
        super().__init__(
            self._adapter.tool_data(definition, adapter_config(definition))
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
        if self._adapter.adapter_id == "text_transform" and (
            set(params) != {"input"} or not isinstance(params["input"], str)
        ):
            return ToolResult(
                tool_name=self.tool_id, content="Expected one text input", success=False
            )
        if (
            self._adapter.adapter_id == "text_transform"
            and len(params["input"]) > 32768
        ):
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
        if self._adapter.adapter_id == "numeric_formula":
            try:
                config = {k: v for k, v in self._action.items() if k != "type"}
                result = evaluate_formula(config, params)
            except ValueError as exc:
                return ToolResult(
                    tool_name=self.tool_id, content=str(exc), success=False
                )
            return ToolResult(tool_name=self.tool_id, content=str(result), success=True)
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
        self.artifact_store = None
        self.store = RuntimeToolStore(db_path)
        from openjarvis.mcp.runtime_manager import RuntimeMCPManager

        self.mcp = RuntimeMCPManager(
            self.store.path.with_name(f"{self.store.path.stem}_mcp.db")
        )
        self.registry = RuntimeRegistry(self)
        self.capabilities = create_builtin_capability_registry()

    def record(self, row):
        tool = RuntimeTransformTool(row, self)
        adapter_version = tool._adapter.validator_version
        registry = ToolManagementRegistry()
        record = sync_managed_tool(
            registry,
            tool,
            identity=tool.management_identity,
            provenance=Provenance(
                source_type="runtime",
                source_id=row["id"],
                source_version=adapter_version,
            ),
            capability_registry=self.capabilities,
            implementation_id=f"{RuntimeTransformTool.__module__}.{RuntimeTransformTool.__qualname__}",
        )
        if (
            row["validator_version"] == adapter_version
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
        try:
            record = self.record(row)
        except (ValueError, TypeError, KeyError, OverflowError):
            # Unsupported/corrupt definitions must remain manageable without
            # exposing their malformed configuration or granting execution.
            return {
                "id": row["id"],
                "revision": row["revision"],
                "name": row["name"],
                "description": "Definition needs repair",
                "adapter_id": "unavailable",
                "config": {},
                "enabled": False,
                "approved": False,
                "approved_by": "",
                "status": "invalid",
                "fingerprint": "",
                "required_capabilities": [],
                "validator_version": "",
                "validation_error": (
                    "The adapter or definition is unavailable. "
                    "Edit or remove this tool."
                ),
            }
        definition = json.loads(row["definition"])
        adapter = get_adapter(definition)
        return {
            "id": row["id"],
            "revision": row["revision"],
            **definition,
            "adapter_id": adapter.adapter_id,
            "config": adapter_config(definition),
            "enabled": bool(row["enabled"]),
            "approved": bool(record.approval),
            "approved_by": row["approved_by"],
            "status": record.status.value,
            "fingerprint": record.fingerprint.value,
            "required_capabilities": record.spec.required_capabilities,
            "validator_version": adapter.validator_version,
        }

    def change(self, identity, revision, event, actor, definition=None):
        uuid.UUID(identity)
        row = self.store.get(identity)
        fingerprint = (
            self.record(row).fingerprint.value
            if event in {"approved", "enabled"}
            else ""
        )
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
            try:
                if self.record(row).is_approved():
                    tools.append(RuntimeTransformTool(row, self))
            except (ValueError, TypeError, KeyError, OverflowError):
                continue
        from openjarvis.core.correlation import current_identity
        from openjarvis.tools.artifact_save import ArtifactSaveTool

        identity = current_identity()
        if (
            self.artifact_store is not None and identity is not None
            and identity.user_id
        ):
            tools.append(ArtifactSaveTool(self.artifact_store))
        return tools + self.mcp.available()

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
        replacements = {tool.spec.name for tool in runtime_tools
                        if tool.spec.name == "artifact_save"}
        tools = [tool for tool in old_tools if tool.spec.name not in replacements]
        names = {tool.spec.name for tool in tools}
        tools.extend(tool for tool in runtime_tools if tool.spec.name not in names)
        agent._tools = tools
        executor._tools = {tool.spec.name: tool for tool in tools}
        try:
            yield
        finally:
            agent._tools, executor._tools = old_tools, old_dispatch
