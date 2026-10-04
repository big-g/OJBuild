"""Runtime definitions persist, and execution rechecks current approval."""

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from openjarvis.agents.tool_resolver import resolve_agent_tools
from openjarvis.core.types import ToolCall
from openjarvis.tools._stubs import ToolExecutor
from openjarvis.tools.runtime_manager import RuntimeToolManager, RuntimeTransformTool
from openjarvis.tools.runtime_store import RuntimeToolConflict


def definition(transform="upper"):
    return {
        "name": "custom_text",
        "description": "Transform supplied text",
        "transform": transform,
    }


def install(manager):
    row = manager.store.create(definition(), "user:admin")
    return manager.change(row["id"], row["revision"], "approved", "user:admin")


def call(executor, input="Hello"):
    return executor.execute(
        ToolCall(id="run", name="custom_text", arguments=json.dumps({"input": input}))
    )


def test_restart_restores_approval_and_cross_process_revocation(tmp_path):
    path = tmp_path / "tools.db"
    manager = RuntimeToolManager(path)
    row = manager.store.create(definition(), "user:admin")
    unapproved = RuntimeTransformTool(row, manager)
    assert manager.available() == []
    assert not call(ToolExecutor([unapproved])).success
    assert not unapproved.execute(input="Hello").success
    row = manager.change(row["id"], 1, "approved", "user:admin")

    restarted = RuntimeToolManager(path)
    tool = restarted.available()[0]
    executor = ToolExecutor([tool])  # Global lifecycle enforcement can be off.
    assert call(executor).content == "HELLO"
    assert tool.spec.evidence_kinds == []
    assert manager.view(row)["approved_by"] == "user:admin"

    row = manager.change(row["id"], row["revision"], "disabled", "user:admin")
    assert not call(executor).success
    assert not tool.execute(input="Hello").success
    assert restarted.available() == []
    row = manager.change(row["id"], row["revision"], "enabled", "user:admin")
    assert call(executor).success
    row = manager.change(
        row["id"], row["revision"], "updated", "user:admin", definition("lower")
    )
    assert not call(executor).success
    assert not manager.view(row)["approved"]
    with pytest.raises(ValueError, match="approval"):
        manager.change(row["id"], row["revision"], "enabled", "user:admin")
    row = manager.change(row["id"], row["revision"], "approved", "user:admin")
    assert not call(executor).success  # Old instance cannot use replacement approval.
    assert call(ToolExecutor(restarted.available())).content == "hello"
    manager.change(row["id"], row["revision"], "deleted", "user:admin")
    assert not call(executor).success
    assert restarted.available() == []
    assert [e["event"] for e in manager.store.audit(row["id"])] == [
        "deleted",
        "approved",
        "updated",
        "enabled",
        "disabled",
        "approved",
        "created",
    ]


def test_stale_reviews_and_concurrent_edits_are_atomic(tmp_path):
    manager = RuntimeToolManager(tmp_path / "tools.db")
    row = install(manager)

    def edit(transform):
        try:
            return manager.change(
                row["id"],
                row["revision"],
                "updated",
                "user:admin",
                definition(transform),
            )
        except RuntimeToolConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(edit, ["lower", "reverse"]))
    assert len([r for r in rows if r]) == 1
    current = manager.store.get(row["id"])
    assert current["revision"] == row["revision"] + 1
    assert current["approved_fingerprint"] == ""
    with pytest.raises(RuntimeToolConflict):
        manager.change(row["id"], row["revision"], "approved", "user:admin")
    assert len(manager.store.audit(row["id"])) == 3


@pytest.mark.parametrize(
    "change",
    [
        {"name": "web_search"},
        {"transform": "shell"},
        {"command": "echo unsafe"},
        {"parameters": {}},
        {"required_capabilities": ["none"]},
        {"description": " "},
    ],
)
def test_definitions_cannot_install_executable_or_forged_contracts(tmp_path, change):
    manager = RuntimeToolManager(tmp_path / "tools.db")
    with pytest.raises(ValueError):
        manager.store.create({**definition(), **change}, "user:admin")
    assert manager.store.list() == []


def test_bounds_and_damaged_persisted_approval_fail_closed(tmp_path):
    manager = RuntimeToolManager(tmp_path / "tools.db")
    row = install(manager)
    tool = manager.available()[0]
    executor = ToolExecutor([tool])
    assert not call(executor, 1).success
    assert not call(executor, "x" * 32769).success
    assert call(executor, "x" * 32768).success
    with manager.store.connection() as db:
        db.execute(
            "UPDATE runtime_tools SET validator_version='old' WHERE id=?", (row["id"],)
        )
    assert not call(executor).success
    assert manager.available() == []
    with manager.store.connection() as db:
        db.execute(
            "UPDATE runtime_tools SET definition='bad json' WHERE id=?", (row["id"],)
        )
    assert not call(executor).success


def test_runtime_resolution_and_temporary_chat_binding(tmp_path):
    manager = RuntimeToolManager(tmp_path / "tools.db")
    row = install(manager)
    resolved = resolve_agent_tools(
        {"config": {"tools": ["custom_text", "custom_text"], "mcp_tools": False}},
        engine=None,
        model="test",
        runtime_tools=manager.available(),
    )
    assert [spec["function"]["name"] for spec in resolved.openai_specs] == [
        "custom_text"
    ]
    assert call(ToolExecutor(resolved.instances)).content == "HELLO"
    agent = SimpleNamespace(_tools=[], _executor=ToolExecutor([]))
    original_dispatch = agent._executor._tools
    with pytest.raises(RuntimeError), manager.bind_agent(agent):
        assert call(agent._executor).content == "HELLO"
        manager.change(row["id"], row["revision"], "disabled", "user:admin")
        assert not call(agent._executor).success
        raise RuntimeError("Restore after failed chat")
    assert agent._tools == []
    assert agent._executor._tools is original_dispatch
    with manager.bind_agent(agent):
        assert agent._tools == []
