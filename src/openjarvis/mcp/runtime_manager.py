"""Web-managed MCP tools use the existing executor and mandatory live approval."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import quote

from openjarvis.core.types import ToolResult
from openjarvis.mcp import catalog
from openjarvis.mcp.client import MCPClient
from openjarvis.mcp.runtime_store import (
    VERSION,
    RuntimeMCPStore,
    canonical,
    definition,
    fingerprint,
)
from openjarvis.mcp.runtime_transport import RuntimeHTTPTransport
from openjarvis.security.capability_registry import (
    ApprovalRecord,
    Provenance,
    ResourceStatus,
    create_builtin_capability_registry,
)
from openjarvis.security.tool_management_bootstrap import sync_managed_tool
from openjarvis.security.tool_management_registry import ToolManagementRegistry
from openjarvis.tools._stubs import ToolSpec
from openjarvis.tools.mcp_adapter import MCPToolAdapter
from openjarvis.tools.runtime_store import RuntimeToolConflict


def snapshot(specs):
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
            "metadata": spec.metadata,
        }
        for spec in specs
    ]


def reject_reflected_token(value, token):
    if token:
        encoded = canonical(value)
        if canonical(token)[1:-1] in encoded or quote(token, safe="") in encoded:
            raise ValueError("MCP endpoint reflected a configured credential")


def saved_specs(row):
    config = definition(json.loads(row["definition"]))
    if config["name"] != row["name"]:
        raise ValueError("Saved MCP connection name is invalid")
    if len(row["catalog"]) > 3 * 1024 * 1024:
        raise ValueError("Saved MCP catalog exceeds size limit")
    data = json.loads(row["catalog"])
    if not isinstance(data, list) or len(data) > catalog.MAX_TOOLS:
        raise ValueError("Saved MCP catalog is invalid")
    specs, names = [], set()
    for item in data:
        if not isinstance(item, dict) or set(item) != {
            "name",
            "description",
            "parameters",
            "metadata",
        }:
            raise ValueError("Saved MCP tool is invalid")
        metadata = item["metadata"]
        if (
            not isinstance(metadata, dict)
            or metadata.get("mcp_contract_version") != "catalog-v1"
        ):
            raise ValueError("Saved MCP contract version is invalid")
        parsed, _ = catalog.parse_tool(
            {
                "name": item["name"],
                "description": item["description"],
                "inputSchema": item["parameters"],
                "annotations": metadata.get("mcp_annotations_untrusted", {}),
                "savedMetadata": metadata,
            }
        )
        if parsed.name in names:
            raise ValueError("Saved MCP catalog contains duplicate names")
        names.add(parsed.name)
        specs.append(ToolSpec(**item, timeout_seconds=60.0))
    return specs


def alias(row, remote):
    readable = re.sub(r"[^a-z0-9_]", "_", remote.lower()).strip("_")[:15] or "tool"
    digest = hashlib.sha256(remote.encode()).hexdigest()[:10]
    return f"custom_mcp_{row['name']}_{readable}_{digest}"


class RuntimeMCPClient(MCPClient):
    def initialize(self):
        info = self._send(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "openjarvis", "version": "0.1.0"},
            },
        ).result
        if not isinstance(info, dict) or (
            info.get("protocolVersion")
            not in {
                "2025-03-26",
                "2025-06-18",
                "2025-11-25",
            }
            or not isinstance(info.get("capabilities"), dict)
            or not isinstance(info["capabilities"].get("tools"), dict)
        ):
            raise ValueError("MCP endpoint has an unsupported tool protocol")
        self._transport.protocol_version = info["protocolVersion"]
        self._capabilities = info["capabilities"]
        self._initialized = True
        self.notify("notifications/initialized")
        return info


class RuntimeMCPTool(MCPToolAdapter):
    def __init__(self, manager, row, spec, *, connection_fingerprint=None):
        super().__init__(None, spec, source_id=row["id"])
        self.manager = manager
        self.connection_id = row["id"]
        self.remote_name = spec.name
        self.connection_fingerprint = connection_fingerprint or fingerprint(row)
        self._spec.name = alias(row, spec.name)
        self._spec.timeout_seconds = 60.0
        self._spec.requires_confirmation = not json.loads(row["definition"])[
            "allow_without_confirmation"
        ]
        self._spec.evidence_kinds = []
        self._spec.metadata["runtime_mcp_fingerprint"] = self.connection_fingerprint
        self._spec.metadata["runtime_mcp_remote_name"] = self.remote_name
        self.management_identity = f"runtime-mcp:{row['id']}:{spec.name}"
        self.runtime_management_registry = manager.registry

    def execute(self, **params):
        try:
            row = self.manager.allowed(self)
            with self.manager.session(row) as client:
                live = snapshot(client.list_tools())
                # Catalog order has no bearing on the contract review.
                live.sort(key=lambda tool: tool["name"])
                if live != json.loads(row["catalog"]):
                    try:
                        self.manager.store.change(
                            row["id"],
                            row["revision"],
                            "discovery_failed",
                            "runtime",
                        )
                    except (KeyError, RuntimeToolConflict):
                        pass  # A concurrent edit/revocation already superseded us.
                    raise ValueError("Remote catalog changed")
                self.manager.allowed(self)
                result = client.call_tool(self.remote_name, params)
                reject_reflected_token(result, self.manager.store.token(row))
                if not isinstance(result, dict) or not isinstance(
                    result.get("content"), list
                ):
                    raise ValueError("Invalid MCP tool result")
                if "isError" in result and not isinstance(result["isError"], bool):
                    raise ValueError("Invalid MCP error flag")
                parts = result["content"]
                if any(not isinstance(part, dict) for part in parts):
                    raise ValueError("Invalid MCP content")
                texts = [part["text"] for part in parts if part.get("type") == "text"]
                if any(not isinstance(text, str) for text in texts):
                    raise ValueError("Invalid MCP text")
                return ToolResult(
                    tool_name=self._spec.name,
                    content="\n".join(texts),
                    success=not result.get("isError", False),
                )
        except Exception:
            # Provider errors may echo credentials, paths or arbitrary payloads.
            return ToolResult(
                tool_name=self._spec.name,
                success=False,
                content=(
                    "MCP tool unavailable: check connection, "
                    "catalog approval and credentials."
                ),
            )


class RuntimeMCPRegistry(ToolManagementRegistry):
    def __init__(self, manager):
        super().__init__()
        self.manager = manager

    def get(self, identity):
        if not identity.startswith("runtime-mcp:"):
            return None
        _, connection_id, remote = identity.split(":", 2)
        try:
            row = self.manager.store.get(connection_id)
        except KeyError:
            return None
        spec = next((s for s in saved_specs(row) if s.name == remote), None)
        if spec is None:
            return None
        tool = RuntimeMCPTool(self.manager, row, spec)
        registry = ToolManagementRegistry()
        record = sync_managed_tool(
            registry,
            tool,
            identity=identity,
            provenance=Provenance(
                source_type="mcp", source_id=connection_id, source_version=VERSION
            ),
            capability_registry=self.manager.capabilities,
            implementation_id=f"{RuntimeMCPTool.__module__}.{RuntimeMCPTool.__qualname__}",
        )
        if self.manager.store.approved(row):
            registry.approve(
                identity,
                ApprovalRecord(
                    approval_id=f"mcp:{connection_id}:{row['approved_at']}",
                    approved_by=row["approved_by"],
                    timestamp=datetime.fromtimestamp(row["approved_at"], timezone.utc),
                    validation_id=record.validation.validation_id,
                    fingerprint=record.fingerprint,
                ),
            )
            if not row["enabled"]:
                record.status = ResourceStatus.DISABLED
        return record


class RuntimeMCPManager:
    def __init__(self, path):
        self.store = RuntimeMCPStore(path)
        self.capabilities = create_builtin_capability_registry()
        self.registry = RuntimeMCPRegistry(self)
        self._slots = threading.BoundedSemaphore(4)

    @contextmanager
    def session(self, row):
        if not self._slots.acquire(blocking=False):
            raise ValueError("MCP connection operations are busy; retry shortly")
        client = None
        try:
            token = self.store.token(row)
            client = RuntimeMCPClient(
                RuntimeHTTPTransport(json.loads(row["definition"])["url"], token)
            )
            client.initialize()
            yield client
        finally:
            try:
                if client is not None:
                    client.close()
            finally:
                self._slots.release()

    def allowed(self, tool):
        row = self.store.get(tool.connection_id)
        if (
            not row["enabled"]
            or not self.store.approved(row)
            or (fingerprint(row) != tool.connection_fingerprint)
        ):
            raise ValueError("MCP connection approval is no longer current")
        return row

    def discover(self, identity, revision, actor):
        row = self.store.get(identity)
        if row["revision"] != revision:
            raise RuntimeToolConflict("Connection changed; reload before discovering")
        try:
            with self.session(row) as client:
                specs = client.list_tools()
            data = sorted(snapshot(specs), key=lambda spec: spec["name"])
            reject_reflected_token(data, self.store.token(row))
            if len({alias(row, spec["name"]) for spec in data}) != len(data):
                raise ValueError("MCP tool aliases are not unique")
        except Exception:
            self.store.change(identity, revision, "discovery_failed", actor)
            raise ValueError(
                "MCP discovery failed; check endpoint and credentials"
            ) from None
        return self.store.change(identity, revision, "discovered", actor, catalog=data)

    def change(self, identity, revision, event, actor, **kwargs):
        if event in {"approved", "enabled"}:
            row = self.store.get(identity)
            saved_specs(row)
            self.store.token(row)  # Missing keys/credentials cannot be approved.
        return self.store.change(identity, revision, event, actor, **kwargs)

    def available(self):
        tools = []
        for row in self.store.list():
            try:
                if row["enabled"] and self.store.approved(row):
                    digest = fingerprint(row)
                    tools.extend(
                        RuntimeMCPTool(self, row, spec, connection_fingerprint=digest)
                        for spec in saved_specs(row)
                    )
            except (ValueError, TypeError, KeyError):
                continue
        return tools

    def view(self, row):
        try:
            return self._view(row)
        except (ValueError, TypeError, KeyError, OverflowError):
            return {
                "id": row["id"],
                "revision": row["revision"],
                "name": row["name"],
                "url": "",
                "allow_without_confirmation": False,
                "has_token": bool(row["token_revision"]),
                "discovered": False,
                "enabled": False,
                "approved": False,
                "approved_by": "",
                "fingerprint": "",
                "tools": [],
                "validation_error": "Connection needs repair. Edit or remove it.",
            }

    def _view(self, row):
        config = json.loads(row["definition"])
        specs = saved_specs(row)
        return {
            "id": row["id"],
            "revision": row["revision"],
            **config,
            "has_token": bool(row["token_revision"]),
            "discovered": bool(row["discovered"]),
            "enabled": bool(row["enabled"]),
            "approved": self.store.approved(row),
            "approved_by": row["approved_by"],
            "fingerprint": fingerprint(row),
            "tools": [
                {
                    "name": alias(row, spec.name),
                    "remote_name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                    "annotations": spec.metadata.get("mcp_annotations_untrusted", {}),
                    "contract_digest": spec.metadata.get("mcp_contract_sha256", ""),
                }
                for spec in specs
            ],
        }
