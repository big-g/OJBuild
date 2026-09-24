from openjarvis.security.capabilities import (
    CapabilityPolicy,
    CapabilityResolutionError,
)
from openjarvis.tools._stubs import BaseTool, ToolSpec


class _DynamicTool(BaseTool):
    tool_id = "dynamic_test"

    def __init__(self, resolved):
        self._resolved = resolved

    @property
    def spec(self):
        return ToolSpec(
            name="dynamic_test",
            description="test",
            required_capabilities=["file:write", "network:fetch"],
        )

    def resolve_required_capabilities(self, params):
        return self._resolved

    def execute(self, **params):
        raise AssertionError("not executed")


def test_runtime_capabilities_may_narrow_declared_envelope():
    policy = CapabilityPolicy()

    assert policy.resolve_effective_tool_capabilities(
        _DynamicTool(("file:write",)),
        {},
    ) == ("file:write",)


def test_runtime_capabilities_may_resolve_to_empty_set():
    policy = CapabilityPolicy()

    assert policy.resolve_effective_tool_capabilities(
        _DynamicTool(()),
        {},
    ) == ()


def test_runtime_capabilities_cannot_expand_declared_envelope():
    policy = CapabilityPolicy()

    try:
        policy.resolve_effective_tool_capabilities(
            _DynamicTool(("code:execute",)),
            {},
        )
    except CapabilityResolutionError as exc:
        assert "expand beyond" in str(exc)
    else:
        raise AssertionError("undeclared capability expansion was accepted")



def test_runtime_capability_may_narrow_wildcard_envelope():
    class _ConnectorTool(BaseTool):
        tool_id = "connector_dynamic"

        @property
        def spec(self):
            return ToolSpec(
                name="connector_dynamic",
                description="connector test",
                required_capabilities=["connector:*:read"],
            )

        def resolve_required_capabilities(self, params):
            return ("connector:gmail:read",)

        def execute(self, **params):
            raise AssertionError("not executed")

    policy = CapabilityPolicy()
    assert policy.resolve_effective_tool_capabilities(
        _ConnectorTool(),
        {},
    ) == ("connector:gmail:read",)
