"""RBAC capability system — fine-grained permission model for tool dispatch."""

from __future__ import annotations

import fnmatch
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from openjarvis.security.capability_registry import (
    CapabilityRegistry,
    create_builtin_capability_registry,
)

logger = logging.getLogger(__name__)


class Capability(str, Enum):
    """Compatibility enum for the canonical built-in capability vocabulary."""

    FILE_READ = "file:read"
    FILE_WRITE = "file:write"
    NETWORK_FETCH = "network:fetch"
    CODE_EXECUTE = "code:execute"
    MEMORY_READ = "memory:read"
    MEMORY_WRITE = "memory:write"
    CHANNEL_SEND = "channel:send"
    TOOL_INVOKE = "tool:invoke"
    SCHEDULE_CREATE = "schedule:create"
    SYSTEM_ADMIN = "system:admin"


@dataclass(slots=True)
class CapabilityGrant:
    """A single capability grant for an agent."""

    capability: str  # Capability value or glob pattern
    pattern: str = "*"  # resource glob pattern


@dataclass(slots=True)
class AgentPolicy:
    """Policy for a specific agent."""

    agent_id: str
    grants: List[CapabilityGrant] = field(default_factory=list)
    deny: List[str] = field(default_factory=list)  # explicit denials


class CapabilityResolutionError(ValueError):
    """Raised when a tool's effective capability requirements are unknown."""


class CapabilityPolicy:
    """RBAC capability policy for tool dispatch.

    The capability registry is authoritative for the capability vocabulary.
    Registration/discovery does not authorize anything; this class only
    evaluates grants and denials for an agent.
    """

    def __init__(
        self,
        *,
        policy_path: Optional[str] = None,
        default_deny: bool = False,
        registry: CapabilityRegistry | None = None,
    ) -> None:
        self._policies: Dict[str, AgentPolicy] = {}
        self._default_deny = default_deny
        self._registry = registry or create_builtin_capability_registry()

        from openjarvis._rust_bridge import get_rust_module

        _rust = get_rust_module()
        self._rust_impl = _rust.CapabilityPolicy(default_deny=default_deny)

        if policy_path:
            self._load_file(Path(policy_path))

    @property
    def registry(self) -> CapabilityRegistry:
        """Return the authoritative capability registry used by this policy."""
        return self._registry

    def _validate_capability_pattern(self, capability: str) -> None:
        """Reject policy entries that do not reference known capabilities."""
        self._registry.require_pattern(capability)

    def resolve_tool_capabilities(self, tool_spec: Any) -> tuple[str, ...]:
        """Resolve a tool's effective capability requirements.

        ``ToolSpec.required_capabilities`` has three states:

        - ``[]``: declaration missing/not migrated; resolve through the
          temporary legacy mapping.
        - ``["none"]``: explicitly capability-free.
        - one or more capability names: explicit requirements.

        The legacy name-based mapping remains temporary and is only consulted
        when the ToolSpec declaration is empty.
        """
        name = getattr(tool_spec, "name", None)
        explicit = getattr(tool_spec, "required_capabilities", None)

        if explicit is not None and not isinstance(explicit, (list, tuple)):
            raise CapabilityResolutionError(
                f"Tool '{name or '<unknown>'}' has invalid required_capabilities"
            )

        if explicit:
            values = tuple(str(cap).strip() for cap in explicit)

            if any(not cap for cap in values):
                raise CapabilityResolutionError(
                    f"Tool '{name or '<unknown>'}' has an empty capability declaration"
                )

            if "none" in values:
                if len(values) != 1:
                    raise CapabilityResolutionError(
                        f"Tool '{name or '<unknown>'}' cannot combine 'none' "
                        "with other capabilities"
                    )
                return ()

            capabilities = values
            source = "ToolSpec.required_capabilities"
        else:
            capabilities = tuple(DEFAULT_TOOL_CAPABILITIES.get(name, ()))
            source = "legacy capability mapping"

        if not capabilities:
            raise CapabilityResolutionError(
                f"Tool '{name or '<unknown>'}' has no resolvable capability requirements"
            )

        for capability in capabilities:
            try:
                self._registry.require_pattern(capability)
            except KeyError as exc:
                raise CapabilityResolutionError(
                    f"Tool '{name or '<unknown>'}' declares unknown capability "
                    f"'{capability}' via {source}"
                ) from exc

        return capabilities

    def resolve_effective_tool_capabilities(
        self,
        tool: Any,
        params: dict[str, Any],
    ) -> tuple[str, ...]:
        """Resolve per-call capabilities within the static ToolSpec envelope."""
        declared = tuple(self.resolve_tool_capabilities(tool.spec))
        declared_set = set(declared)

        resolver = getattr(tool, "resolve_required_capabilities", None)
        if not callable(resolver):
            return declared

        try:
            resolved = resolver(params)
        except CapabilityResolutionError:
            raise
        except Exception as exc:
            raise CapabilityResolutionError(
                f"Tool '{tool.spec.name}' runtime capability resolution "
                f"failed: {exc}"
            ) from exc

        if resolved is None:
            return declared

        if (
            isinstance(resolved, str)
            or not isinstance(resolved, (list, tuple, set, frozenset))
        ):
            raise CapabilityResolutionError(
                f"Tool '{tool.spec.name}' returned invalid runtime capabilities"
            )

        effective = tuple(str(cap).strip() for cap in resolved)

        if any(not cap or cap == "none" for cap in effective):
            raise CapabilityResolutionError(
                f"Tool '{tool.spec.name}' returned an invalid runtime capability"
            )

        undeclared = sorted(set(effective) - declared_set)
        if undeclared:
            raise CapabilityResolutionError(
                f"Tool '{tool.spec.name}' attempted to expand beyond its "
                f"declared capability envelope: {', '.join(undeclared)}"
            )

        for capability in effective:
            try:
                self._registry.require_pattern(capability)
            except KeyError as exc:
                raise CapabilityResolutionError(
                    f"Tool '{tool.spec.name}' resolved unknown capability "
                    f"'{capability}'"
                ) from exc

        return tuple(dict.fromkeys(effective))

    def grant(self, agent_id: str, capability: str, pattern: str = "*") -> None:
        """Grant a known capability (or known capability glob) to an agent."""
        self._validate_capability_pattern(capability)
        policy = self._policies.setdefault(
            agent_id,
            AgentPolicy(agent_id=agent_id),
        )
        policy.grants.append(CapabilityGrant(capability=capability, pattern=pattern))
        self._rust_impl.grant(agent_id, capability, pattern)

    def deny(self, agent_id: str, capability: str) -> None:
        """Explicitly deny a known capability (or known capability glob) to an agent."""
        self._validate_capability_pattern(capability)
        policy = self._policies.setdefault(
            agent_id,
            AgentPolicy(agent_id=agent_id),
        )
        policy.deny.append(capability)
        self._rust_impl.deny(agent_id, capability)

    def check(self, agent_id: str, capability: str, resource: str = "") -> bool:
        """Check whether *agent_id* has *capability* for *resource*.

        Unknown capabilities fail closed rather than being evaluated by the
        underlying policy implementation.
        """
        if not self._registry.contains(capability):
            return False
        return self._rust_impl.check(agent_id, capability, resource)

    def _check_python(self, agent_id: str, capability: str, resource: str = "") -> bool:
        """Legacy Python check — kept for reference only."""
        if not self._registry.contains(capability):
            return False

        policy = self._policies.get(agent_id)
        if policy is None:
            return not self._default_deny

        for denied in policy.deny:
            if fnmatch.fnmatch(capability, denied):
                return False

        for grant in policy.grants:
            if fnmatch.fnmatch(capability, grant.capability):
                if resource and grant.pattern != "*":
                    if fnmatch.fnmatch(resource, grant.pattern):
                        return True
                else:
                    return True

        return not self._default_deny

    def list_grants(self, agent_id: str) -> List[CapabilityGrant]:
        """List all grants for an agent."""
        policy = self._policies.get(agent_id)
        return list(policy.grants) if policy else []

    def list_agents(self) -> List[str]:
        """List all agents with explicit policies."""
        return list(self._policies.keys())

    def _load_file(self, path: Path) -> None:
        """Load policy from a JSON file."""
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            for agent_data in data.get("agents", []):
                agent_id = agent_data["agent_id"]
                for grant_data in agent_data.get("grants", []):
                    self.grant(
                        agent_id,
                        grant_data["capability"],
                        grant_data.get("pattern", "*"),
                    )
                for denied in agent_data.get("deny", []):
                    self.deny(agent_id, denied)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Failed to parse capability policy: %s", exc)

    def save(self, path: Path) -> None:
        """Save policy to a JSON file."""
        agents = []
        for agent_id, policy in self._policies.items():
            agents.append(
                {
                    "agent_id": agent_id,
                    "grants": [
                        {"capability": g.capability, "pattern": g.pattern}
                        for g in policy.grants
                    ],
                    "deny": policy.deny,
                }
            )
        path.write_text(json.dumps({"agents": agents}, indent=2))


# Compatibility mapping retained temporarily for callers that have not yet
# migrated to ToolSpec.required_capabilities. New code should declare the
# capability on the ToolSpec and resolve it through the registry.
DEFAULT_TOOL_CAPABILITIES: Dict[str, List[str]] = {
    "file_read": [Capability.FILE_READ],
    "web_search": [Capability.NETWORK_FETCH],
    "code_interpreter": [Capability.CODE_EXECUTE],
    "memory_store": [Capability.MEMORY_WRITE],
    "memory_retrieve": [Capability.MEMORY_READ],
    "memory_search": [Capability.MEMORY_READ],
    "memory_index": [Capability.MEMORY_WRITE],
    "schedule_task": [Capability.SCHEDULE_CREATE],
    "channel_send": [Capability.CHANNEL_SEND],
}


__all__ = [
    "AgentPolicy",
    "Capability",
    "CapabilityGrant",
    "CapabilityPolicy",
    "CapabilityResolutionError",
    "DEFAULT_TOOL_CAPABILITIES",
]
