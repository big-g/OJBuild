"""Template approval binds executable behavior, not only its public schema."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from openjarvis.core.types import ToolCall
from openjarvis.security.capability_registry import (
    ApprovalRecord,
    ResourceStatus,
    create_builtin_capability_registry,
)
from openjarvis.security.tool_management_registry import ToolManagementRegistry
from openjarvis.tools._stubs import ToolExecutor
from openjarvis.tools.templates.loader import ToolTemplate, load_template


def approve(registry, tool):
    record = registry.require(tool.management_identity)
    registry.approve(
        record.identity,
        ApprovalRecord(
            approval_id="review",
            approved_by="reviewer",
            timestamp=datetime.now(timezone.utc),
            validation_id=record.validation.validation_id,
            fingerprint=record.fingerprint,
        ),
    )
    return record


@pytest.mark.parametrize(
    "action,field,replacement",
    [
        ({"type": "python", "expression": "1 + 1"}, "expression", "1 + 2"),
        ({"type": "shell", "command": "echo first"}, "command", "echo second"),
        ({"type": "transform", "transform": "upper"}, "transform", "lower"),
    ],
)
def test_action_edit_requires_new_approval(action, field, replacement):
    registry = ToolManagementRegistry()
    kwargs = dict(
        management_registry=registry,
        capability_registry=create_builtin_capability_registry(),
        source_id="same-template.toml",
    )
    data = {"name": "reviewed", "action": action}
    first = ToolTemplate(data, **kwargs)
    record = approve(registry, first)
    fingerprint = record.fingerprint
    validation_id = record.validation.validation_id

    # Object/key order has no effect on the approval.
    reordered = deepcopy(data)
    reordered["action"] = dict(reversed(list(action.items())))
    same = ToolTemplate(reordered, **kwargs)
    assert record.status == ResourceStatus.APPROVED
    assert record.validation.validation_id == validation_id

    data["action"][field] = replacement
    changed = ToolTemplate(data, **kwargs)
    assert record.fingerprint != fingerprint
    assert record.status == ResourceStatus.VALIDATED
    assert record.approval is None
    assert record.validation.validation_id != validation_id
    executor = ToolExecutor([changed], tool_management_registry=registry)
    result = executor.execute(ToolCall(id="blocked", name="reviewed", arguments="{}"))
    assert not result.success
    assert "not approved" in result.content
    assert same.spec.metadata != changed.spec.metadata


def test_definition_and_returned_spec_are_detached_from_callers():
    data = {
        "name": "snapshot",
        "parameters": {"type": "object", "properties": {"input": {"type": "string"}}},
        "action": {"type": "transform", "transform": "upper"},
    }
    tool = ToolTemplate(data)
    original = tool.spec
    data["action"]["transform"] = "lower"
    data["parameters"]["properties"]["input"]["type"] = "number"
    original.parameters["properties"].clear()
    assert tool.execute(input="Hello").content == "HELLO"
    assert tool.spec.parameters["properties"]["input"]["type"] == "string"
    assert tool.spec.metadata == original.metadata


def test_live_action_mutation_is_blocked_before_execution():
    registry = ToolManagementRegistry()
    tool = ToolTemplate(
        {"name": "live", "action": {"type": "transform", "transform": "upper"}},
        management_registry=registry,
        capability_registry=create_builtin_capability_registry(),
    )
    approve(registry, tool)
    tool._action["transform"] = "lower"
    executor = ToolExecutor([tool], tool_management_registry=registry)
    result = executor.execute(
        ToolCall(id="changed", name="live", arguments='{"input":"Hello"}')
    )
    assert not result.success
    assert "definition changed" in result.content.lower()


def test_approved_transform_executes_through_management_gate():
    registry = ToolManagementRegistry()
    tool = ToolTemplate(
        {"name": "approved", "action": {"type": "transform", "transform": "upper"}},
        management_registry=registry,
        capability_registry=create_builtin_capability_registry(),
    )
    approve(registry, tool)
    executor = ToolExecutor([tool], tool_management_registry=registry)
    result = executor.execute(
        ToolCall(id="allowed", name="approved", arguments='{"input":"Hello"}')
    )
    assert result.success
    assert result.content == "HELLO"


def test_file_reload_preserves_only_unchanged_approval(tmp_path):
    path = tmp_path / "tool.toml"
    definition = (
        '[tool]\nname="file_tool"\n'
        '[tool.action]\ntype="python"\nexpression="1"\n'
    )
    path.write_text(definition)
    registry = ToolManagementRegistry()
    kwargs = dict(
        management_registry=registry,
        capability_registry=create_builtin_capability_registry(),
    )
    tool = load_template(path, **kwargs)
    record = approve(registry, tool)
    path.write_text(definition.replace('expression="1"', 'expression="2"'))
    # Already loaded instances retain the reviewed definition.
    assert tool.execute().content == "1"
    changed = load_template(path, **kwargs)
    assert record.approval is None
    assert changed.execute().content == "2"
