"""Offline discovery contracts: completeness, boundedness and approval integrity."""

from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from openjarvis.mcp import catalog
from openjarvis.mcp.client import MCPClient
from openjarvis.mcp.protocol import MCPResponse
from openjarvis.tools.mcp_adapter import MCPToolAdapter, MCPToolProvider


def entry(name="lookup"):
    return {
        "name": name, "description": "Find something",
        "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
        "annotations": {"readOnlyHint": True},
    }


def client_for(*pages):
    transport = MagicMock()
    transport.send.side_effect = [MCPResponse(result=page) for page in pages]
    return MCPClient(transport), transport


def test_full_catalog_with_empty_intermediate_page():
    client, transport = client_for(
        {"tools": [entry()], "nextCursor": "a"},
        {"tools": [], "nextCursor": "b"},
        {"tools": [entry("second")]},
    )
    assert [tool.name for tool in client.list_tools()] == ["lookup", "second"]
    assert [call.args[0].params for call in transport.send.call_args_list] == [
        {}, {"cursor": "a"}, {"cursor": "b"},
    ]


@pytest.mark.parametrize("page", [
    None, [], {}, {"tools": {}}, {"tools": [None]},
    {"tools": [entry()], "nextCursor": ""},
    {"tools": [entry()], "nextCursor": 3},
    {"tools": [], "nextCursor": "x" * 4097},
])
def test_bad_pages_never_register_partial_tools(page):
    client, _ = client_for({"tools": [entry()], "nextCursor": "a"}, page)
    managed = MagicMock()
    provider = MCPToolProvider(
        client, management_registry=managed, capability_registry=MagicMock(),
    )
    with pytest.raises(ValueError):
        provider.discover()
    managed.register.assert_not_called()
    managed.get.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"name": ""}, {"name": " "}, {"name": "x" * 129}, {"name": 1},
    {"description": None}, {"inputSchema": None},
    {"inputSchema": {"type": "array"}},
    {"inputSchema": {"properties": []}},
    {"inputSchema": {"required": ["q", "q"]}},
    {"inputSchema": {"required": [1]}},
    {"inputSchema": {"properties": {"q": {"type": "invalid"}}}},
    {"inputSchema": {"additionalProperties": 1}},
    {"inputSchema": {"$ref": "https://example.com/schema"}},
    {"annotations": []}, {"annotations": {"readOnlyHint": "true"}},
    {"annotations": {"title": 1}}, {"extension": float("nan")},
])
def test_malformed_contract(changes):
    tool = entry() | changes
    with pytest.raises(ValueError):
        catalog.parse_tool(tool)


def test_duplicate_names_and_cursor_loops_fail():
    client, _ = client_for(
        {"tools": [entry()], "nextCursor": "a"}, {"tools": [entry()]},
    )
    with pytest.raises(ValueError, match="duplicate"):
        client.list_tools()
    client, transport = client_for(
        {"tools": [], "nextCursor": "a"}, {"tools": [], "nextCursor": "a"},
    )
    with pytest.raises(ValueError, match="repeats"):
        client.list_tools()
    assert transport.send.call_count == 2


@pytest.mark.parametrize("limit,value,pages,message", [
    ("MAX_PAGES", 1, [{"tools": [], "nextCursor": "a"}], "page limit"),
    ("MAX_TOOLS", 1, [{"tools": [entry(), entry("second")]}], "tool limit"),
    ("MAX_TOOL_BYTES", 50, [{"tools": [entry()]}], "size limit"),
    ("MAX_CATALOG_BYTES", 250, [{"tools": [entry(), entry("second")]}], "size limit"),
    ("MAX_JSON_NODES", 3, [{"tools": [entry()]}], "structural limits"),
    ("MAX_JSON_DEPTH", 2, [{"tools": [entry()]}], "structural limits"),
])
def test_limits_fail_closed(monkeypatch, limit, value, pages, message):
    monkeypatch.setattr(catalog, limit, value)
    client, _ = client_for(*pages)
    with pytest.raises(ValueError, match=message):
        client.list_tools()


def test_contract_and_adapter_copies_are_isolated():
    wire = entry()
    spec, _ = catalog.parse_tool(wire)
    client = MagicMock()
    adapter = MCPToolAdapter(client, spec)
    original = deepcopy(adapter.spec)
    wire["inputSchema"]["properties"]["q"]["type"] = "number"
    spec.parameters.clear()
    spec.metadata.clear()
    returned = adapter.spec
    returned.name = "different"
    returned.parameters.clear()
    returned.required_capabilities.clear()
    assert adapter.spec == original
    assert adapter.spec.required_capabilities == ["tool:invoke"]
    assert adapter.spec.evidence_kinds == []
    assert adapter.spec.metadata["mcp_annotations_untrusted"] == {"readOnlyHint": True}
    client.call_tool.return_value = {"content": []}
    adapter.execute(q="hi")
    client.call_tool.assert_called_once_with("lookup", {"q": "hi"})


@pytest.mark.parametrize("change", [
    {"description": "New behavior"}, {"annotations": {"readOnlyHint": False}},
    {"inputSchema": {"type": "object", "properties": {"q": {"type": "number"}}}},
    {"futureExtension": {"behavior": "new"}},
])
def test_remote_contract_changes_withdraw_approval(change):
    from openjarvis.security.capability_registry import (
        ApprovalRecord,
        create_builtin_capability_registry,
    )
    from openjarvis.security.tool_management_registry import ToolManagementRegistry

    client, _ = client_for(
        {"tools": [entry()]}, {"tools": [entry()]}, {"tools": [entry() | change]},
    )
    managed = ToolManagementRegistry()
    provider = MCPToolProvider(
        client, source_id="server", management_registry=managed,
        capability_registry=create_builtin_capability_registry(),
    )
    provider.discover()
    identity = "mcp:server:lookup"
    record = managed.require(identity)
    managed.approve(identity, ApprovalRecord(
        approval_id="approval", approved_by="admin",
        timestamp=datetime.now(timezone.utc),
        validation_id=record.validation.validation_id,
        fingerprint=record.fingerprint,
    ))
    provider.discover()
    assert managed.require(identity).approval is not None
    provider.discover()
    assert managed.require(identity).approval is None


def test_legacy_empty_schema_and_local_reference():
    catalog.parse_tool(entry() | {"inputSchema": {}})
    catalog.parse_tool(entry() | {"inputSchema": {
        "type": "object", "$defs": {"query": {"type": "string"}},
        "properties": {"q": {"$ref": "#/$defs/query"}},
    }})
