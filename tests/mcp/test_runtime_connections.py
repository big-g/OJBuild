"""Offline connection lifecycle, credential binding and managed execution."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from openjarvis.core.types import ToolCall, ToolResult
from openjarvis.mcp.runtime_manager import (
    RuntimeMCPManager,
    RuntimeMCPTool,
    saved_specs,
)
from openjarvis.mcp.server import MCPServer
from openjarvis.mcp.transport import InProcessTransport
from openjarvis.tools._stubs import BaseTool, ToolExecutor, ToolSpec
from openjarvis.tools.runtime_manager import RuntimeToolManager
from openjarvis.tools.runtime_store import RuntimeToolConflict


class Echo(BaseTool):
    tool_id = "echo"

    @property
    def spec(self):
        return ToolSpec(
            name="echo",
            description="Repeat input",
            parameters={
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
            },
        )

    def execute(self, **params):
        return ToolResult(tool_name="echo", content=params["input"], success=True)


@pytest.fixture
def remote(monkeypatch):
    server = MCPServer([Echo()])
    state = {"changed": False, "calls": 0, "clients": [], "fail": False}

    class Transport(InProcessTransport):
        def __init__(self, url, token, **kwargs):
            super().__init__(server)
            self.closed = False
            self.token = token
            self.auth = kwargs.get("auth", {})
            state["clients"].append(self)

        def send(self, request):
            if state["fail"]:
                raise ValueError("provider leaked TEST-SECRET")
            if request.method == "tools/call":
                state["calls"] += 1
            result = super().send(request)
            if request.method == "tools/list" and state["changed"]:
                result.result["tools"][0]["annotations"] = {"readOnlyHint": True}
            return result

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        "openjarvis.mcp.runtime_manager.RuntimeHTTPTransport", Transport
    )
    return state


def config(**kwargs):
    return {
        "name": "example",
        "url": "https://example.com/mcp",
        "allow_without_confirmation": True,
    } | kwargs


def installed(manager):
    row = manager.store.create(config(), "admin", "TEST-SECRET")
    row = manager.discover(row["id"], row["revision"], "admin")
    return manager.change(row["id"], row["revision"], "approved", "admin")


def call(tool, **kwargs):
    return ToolExecutor([tool], **kwargs).execute(
        ToolCall(
            id="run",
            name=tool.spec.name,
            arguments='{"input":"Hello"}',
        )
    )


def test_complete_lifecycle_and_cross_process_revocation(tmp_path, remote):
    manager = RuntimeMCPManager(tmp_path / "mcp.db")
    row = manager.store.create(config(), "admin", "TEST-SECRET")
    assert remote["clients"] == []  # Save does not contact the endpoint.
    assert manager.available() == []
    with pytest.raises(ValueError, match="Discover"):
        manager.change(row["id"], 1, "approved", "admin")
    row = manager.discover(row["id"], 1, "admin")
    assert remote["calls"] == 0 and remote["clients"][-1].closed
    unapproved = RuntimeMCPTool(manager, row, saved_specs(row)[0])
    assert not call(unapproved).success
    row = manager.change(row["id"], row["revision"], "approved", "admin")
    restarted = RuntimeMCPManager(tmp_path / "mcp.db")
    tool = restarted.available()[0]
    assert call(tool).content == "Hello"
    assert tool.spec.required_capabilities == ["tool:invoke"]
    assert not tool.spec.evidence_kinds and not tool.is_local
    assert remote["clients"][-1].closed and remote["clients"][-1].token == "TEST-SECRET"
    row = manager.change(row["id"], row["revision"], "disabled", "admin")
    count = len(remote["clients"])
    assert not call(tool).success and not tool.execute(input="Hi").success
    assert len(remote["clients"]) == count and restarted.available() == []
    row = manager.change(row["id"], row["revision"], "enabled", "admin")
    assert call(tool).success
    row = manager.change(
        row["id"],
        row["revision"],
        "updated",
        "admin",
        config=config(),
        token="ROTATED-SECRET",
    )
    assert not call(tool).success and not manager.view(row)["approved"]
    row = manager.discover(row["id"], row["revision"], "admin")
    row = manager.change(row["id"], row["revision"], "approved", "admin")
    assert not call(tool).success  # A stale instance cannot borrow new approval.
    assert call(restarted.available()[0]).success
    manager.change(row["id"], row["revision"], "deleted", "admin")
    assert restarted.available() == [] and not call(tool).success
    assert manager.store.audit(row["id"])[0]["event"] == "deleted"
    with manager.store.vault._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM connector_tokens").fetchone()[0] == 0


def test_live_catalog_change_blocks_before_invocation(tmp_path, remote):
    manager = RuntimeMCPManager(tmp_path / "mcp.db")
    row = installed(manager)
    tool = manager.available()[0]
    remote["changed"] = True
    assert not call(tool).success
    assert remote["calls"] == 0 and manager.available() == []
    assert not manager.view(manager.store.get(row["id"]))["approved"]
    assert manager.store.audit(row["id"])[0]["actor"] == "runtime"
    assert remote["clients"][-1].closed


def test_unchanged_rediscovery_preserves_approval_and_failure_revokes(tmp_path, remote):
    manager = RuntimeMCPManager(tmp_path / "mcp.db")
    row = installed(manager)
    row = manager.discover(row["id"], row["revision"], "admin")
    assert manager.view(row)["approved"]
    remote["fail"] = True
    with pytest.raises(ValueError, match="discovery failed") as error:
        manager.discover(row["id"], row["revision"], "admin")
    assert "TEST-SECRET" not in str(error.value)
    assert remote["clients"][-1].closed and manager.available() == []


def test_confirmation_and_capabilities_are_independent(tmp_path, remote):
    manager = RuntimeMCPManager(tmp_path / "mcp.db")
    row = manager.store.create(config(allow_without_confirmation=False), "admin")
    row = manager.discover(row["id"], 1, "admin")
    manager.change(row["id"], row["revision"], "approved", "admin")
    tool = manager.available()[0]
    assert not call(tool).success and remote["calls"] == 0
    assert call(tool, interactive=True, confirm_callback=lambda _: True).success
    from openjarvis.security.capabilities import CapabilityPolicy

    policy = CapabilityPolicy(default_deny=True)
    assert not call(
        tool,
        interactive=True,
        confirm_callback=lambda _: True,
        capability_policy=policy,
        agent_id="agent",
    ).success
    assert remote["calls"] == 1


def test_tokens_encrypted_bound_and_missing_key_fails_closed(tmp_path, remote):
    manager = RuntimeMCPManager(tmp_path / "mcp.db")
    row = installed(manager)
    assert b"TEST-SECRET" not in manager.store.path.read_bytes()
    assert "TEST-SECRET" not in json.dumps(manager.view(row))
    assert "TEST-SECRET" not in json.dumps(manager.store.audit(row["id"]))
    assert manager.store.path.stat().st_mode & 0o777 == 0o600
    row = manager.change(
        row["id"], row["revision"], "updated", "admin", config=config(name="renamed")
    )
    assert manager.store.token(row) == "TEST-SECRET"
    manager.store.vault.key_path.unlink()
    with pytest.raises(ValueError, match="discovery failed"):
        manager.discover(row["id"], row["revision"], "admin")
    assert not manager.store.vault.key_path.exists()
    with pytest.raises(ValueError, match="could not be unlocked"):
        manager.store.token(row)


def test_endpoint_change_clears_retained_secret(tmp_path):
    manager = RuntimeMCPManager(tmp_path / "mcp.db")
    row = manager.store.create(config(), "admin", "TEST-SECRET")
    row = manager.change(
        row["id"],
        1,
        "updated",
        "admin",
        config=config(url="https://other.example.com/mcp"),
    )
    assert not manager.view(row)["has_token"] and manager.store.token(row) == ""


def test_revision_races_and_stale_discovery_do_not_contact_remote(tmp_path, remote):
    manager = RuntimeMCPManager(tmp_path / "mcp.db")
    row = installed(manager)

    def edit(name):
        try:
            return manager.change(
                row["id"], row["revision"], "updated", "admin", config=config(name=name)
            )
        except RuntimeToolConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(item is not None for item in pool.map(edit, ["one", "two"])) == 1
    count = len(remote["clients"])
    with pytest.raises(RuntimeToolConflict):
        manager.discover(row["id"], row["revision"], "admin")
    assert len(remote["clients"]) == count


def test_existing_runtime_paths_include_mcp_without_network_on_resolution(
    tmp_path, remote
):
    manager = RuntimeToolManager(tmp_path / "tools.db")
    installed(manager.mcp)
    count = len(remote["clients"])
    tools = manager.available()
    assert len(tools) == 1 and len(remote["clients"]) == count
    from openjarvis.agents.tool_resolver import resolve_agent_tools

    assert (
        resolve_agent_tools(
            {"agent_type": "simple", "config": {"tools": [tools[0].spec.name]}},
            runtime_tools=tools,
            engine=None,
            model="test",
        ).instances[0]
        is tools[0]
    )
    assert call(tools[0]).success


def test_corrupt_catalog_is_quarantined_and_removable(tmp_path, remote):
    manager = RuntimeMCPManager(tmp_path / "mcp.db")
    row = installed(manager)
    with manager.store.vault._connection() as db:
        db.execute("UPDATE runtime_mcp SET catalog=? WHERE id=?", ("broken", row["id"]))
    bad = manager.store.get(row["id"])
    assert manager.available() == []
    assert manager.view(bad)["validation_error"]
    assert not manager.view(bad)["approved"]
    manager.change(row["id"], row["revision"], "deleted", "admin")
    assert manager.store.list() == []


def test_reflected_credentials_are_never_stored_or_returned(
    tmp_path, remote, monkeypatch
):
    manager = RuntimeMCPManager(tmp_path / "mcp.db")
    row = installed(manager)
    tool = manager.available()[0]
    # Even a successfully invoked provider must not return its bearer token.
    result = tool.execute(input="TEST-SECRET")
    assert not result.success and "TEST-SECRET" not in result.content
    from openjarvis.mcp.runtime_manager import RuntimeMCPClient

    original = RuntimeMCPClient.list_tools

    def reflected(client):
        specs = original(client)
        specs[0].description = "TEST-SECRET"
        return specs

    monkeypatch.setattr(RuntimeMCPClient, "list_tools", reflected)
    with pytest.raises(ValueError, match="discovery failed"):
        manager.discover(row["id"], row["revision"], "admin")
    saved = manager.store.get(row["id"])
    assert "TEST-SECRET" not in saved["catalog"]
    assert not manager.available()


def test_edit_during_discovery_cannot_approve_replacement(
    tmp_path, remote, monkeypatch
):
    manager = RuntimeMCPManager(tmp_path / "mcp.db")
    row = installed(manager)
    from openjarvis.mcp.runtime_manager import RuntimeMCPClient

    original = RuntimeMCPClient.list_tools

    def race(client):
        manager.change(
            row["id"],
            row["revision"],
            "updated",
            "admin",
            config=config(name="replacement"),
        )
        return original(client)

    monkeypatch.setattr(RuntimeMCPClient, "list_tools", race)
    with pytest.raises(RuntimeToolConflict):
        manager.discover(row["id"], row["revision"], "admin")
    saved = manager.store.get(row["id"])
    assert not manager.view(saved)["approved"] and saved["name"] == "replacement"


def test_connection_limit_and_duplicate_names_are_atomic(tmp_path):
    manager = RuntimeMCPManager(tmp_path / "mcp.db")
    manager.store.create(config(), "admin")
    with pytest.raises(RuntimeToolConflict):
        manager.store.create(config(), "admin", "TEST-SECRET")
    for index in range(23):
        manager.store.create(config(name=f"server_{index}"), "admin")
    with pytest.raises(ValueError, match="limit"):
        manager.store.create(config(name="overflow"), "admin")
    assert len(manager.store.list()) == 24
    with manager.store.vault._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM connector_tokens").fetchone()[0] == 0


def test_revocation_during_preflight_blocks_remote_call(tmp_path, remote, monkeypatch):
    manager = RuntimeMCPManager(tmp_path / "mcp.db")
    row = installed(manager)
    tool = manager.available()[0]
    from openjarvis.mcp.runtime_manager import RuntimeMCPClient

    original = RuntimeMCPClient.list_tools

    def revoke(client):
        manager.change(row["id"], row["revision"], "disabled", "admin")
        return original(client)

    monkeypatch.setattr(RuntimeMCPClient, "list_tools", revoke)
    assert not call(tool).success
    assert remote["calls"] == 0 and remote["clients"][-1].closed


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/mcp",
        "https://127.0.0.1/mcp",
        "https://10.0.0.1/mcp",
        "https://localhost/mcp",
        "https://user:password@example.com/mcp",
        "https://example.com/mcp?token=secret",
        "https://example.com:8443/mcp",
    ],
)
def test_unsafe_endpoints_rejected_without_connection(tmp_path, remote, url):
    manager = RuntimeMCPManager(tmp_path / "mcp.db")
    with pytest.raises(ValueError):
        manager.store.create(config(url=url), "admin")
    assert manager.store.list() == [] and remote["clients"] == []
