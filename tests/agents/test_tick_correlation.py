"""Managed ticks isolate execution identity across retries and shared-bus events."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import pytest

from openjarvis.agents._stubs import AgentResult
from openjarvis.agents.errors import FatalError, RetryableError
from openjarvis.agents.executor import AgentExecutor
from openjarvis.agents.manager import AgentManager
from openjarvis.core.correlation import (
    ExecutionIdentity,
    current_identity,
    execution_scope,
)
from openjarvis.core.events import EventBus, EventType
from openjarvis.traces.store import TraceStore


@pytest.mark.parametrize("fail", [False, True])
def test_tick_events_trace_response_and_retries_share_identity(tmp_path, fail):
    manager = AgentManager(str(tmp_path / "agents.db"))
    traces = TraceStore(tmp_path / "traces.db")
    bus = EventBus(record_history=True)
    executor = AgentExecutor(manager, bus, trace_store=traces)
    agent = manager.create_agent("test")
    identities = []

    def invoke(agent):
        identities.append(current_identity().metadata())
        if len(identities) == 1:
            raise RetryableError("retry")
        if fail:
            raise FatalError("failed")
        return AgentResult(content="OK")

    ambient = ExecutionIdentity(user_id="request-user", session_id="request-session")
    with execution_scope(ambient), patch.object(executor, "_invoke_agent", invoke):
        with patch("openjarvis.agents.executor.time.sleep"):
            executor.execute_tick(agent["id"])
        assert current_identity() is ambient
    assert identities[0] == identities[1]
    identity = identities[0]
    assert identity["user_id"] == identity["session_id"] == ""
    assert identity["trace_id"] != ambient.trace_id
    assert all(e.correlation == identity for e in bus.history)
    trace = traces.get(identity["trace_id"])
    assert trace.metadata["correlation"] == identity
    assert trace.outcome == ("error" if fail else "success")
    if not fail:
        assert manager.list_messages(agent["id"])[0]["correlation"] == identity
    assert current_identity() is None
    manager.close()
    reopened = AgentManager(str(tmp_path / "agents.db"))
    if not fail:
        assert reopened.list_messages(agent["id"])[0]["correlation"] == identity
    reopened.close()
    traces.close()


def test_late_and_unscoped_tool_events_cannot_enter_next_tick(tmp_path):
    manager = AgentManager(str(tmp_path / "agents.db"))
    traces = TraceStore(tmp_path / "traces.db")
    bus = EventBus(record_history=True)
    executor = AgentExecutor(manager, bus, trace_store=traces)
    agent = manager.create_agent("test")
    identities = []

    def emit(tool):
        bus.publish(EventType.TOOL_CALL_START, {"agent": agent["id"], "tool": tool})
        bus.publish(EventType.TOOL_CALL_END, {"agent": agent["id"], "result": tool})

    def invoke(agent):
        identities.append(current_identity())
        if len(identities) == 2:
            # Includes the same agent ID: only the execution identity differs.
            with execution_scope(identities[0]):
                emit("late")
            with execution_scope(None):
                emit("unscoped")
        emit("current")
        return AgentResult(content="OK")

    with patch.object(executor, "_invoke_agent", invoke), patch.object(
        manager, "update_agent", wraps=manager.update_agent
    ) as update:
        executor.execute_tick(agent["id"])
        executor.execute_tick(agent["id"])
    assert sum("last_activity_at" in call.kwargs for call in update.call_args_list) == 2
    assert identities[0].trace_id != identities[1].trace_id
    for identity in identities:
        trace = traces.get(identity.trace_id)
        assert [s.input["tool"] for s in trace.steps] == ["current"]
        assert trace.steps[0].output["result"] == "current"
    manager.close()
    traces.close()


def test_parallel_ticks_do_not_mix_identities_or_steps(tmp_path):
    bus = EventBus(record_history=True)
    barrier = Barrier(2)

    def run(index):
        manager = AgentManager(str(tmp_path / f"agents-{index}.db"))
        traces = TraceStore(tmp_path / f"traces-{index}.db")
        executor = AgentExecutor(manager, bus, trace_store=traces)
        agent = manager.create_agent(str(index))

        def invoke(agent):
            barrier.wait(timeout=2)
            bus.publish(
                EventType.TOOL_CALL_START, {"agent": agent["id"], "tool": str(index)}
            )
            bus.publish(
                EventType.TOOL_CALL_END, {"agent": agent["id"], "result": str(index)}
            )
            return AgentResult(content=str(index))

        with patch.object(executor, "_invoke_agent", invoke):
            executor.execute_tick(agent["id"])
        message = manager.list_messages(agent["id"])[0]
        identity = message["correlation"]
        trace = traces.get(identity["trace_id"])
        assert [s.input["tool"] for s in trace.steps] == [str(index)]
        assert all(
            e.correlation == identity
            for e in bus.history
            if e.data.get("agent_id", e.data.get("agent")) == agent["id"]
        )
        assert current_identity() is None
        manager.close()
        traces.close()
        return identity

    with ThreadPoolExecutor(max_workers=2) as workers:
        identities = list(workers.map(run, [1, 2]))
    assert identities[0]["trace_id"] != identities[1]["trace_id"]


def test_skipped_tick_preserves_caller_scope(tmp_path):
    manager = AgentManager(str(tmp_path / "agents.db"))
    bus = EventBus(record_history=True)
    executor = AgentExecutor(manager, bus)
    agent = manager.create_agent("test")
    manager.start_tick(agent["id"])
    ambient = ExecutionIdentity()
    with execution_scope(ambient):
        executor.execute_tick(agent["id"])
        assert current_identity() is ambient
    assert bus.history == []
    manager.end_tick(agent["id"])
    manager.close()


def test_legacy_message_migration_preserves_content(tmp_path):
    import sqlite3

    from openjarvis.agents.manager import _CREATE_MESSAGES

    path = tmp_path / "agents.db"
    conn = sqlite3.connect(path)
    conn.executescript(_CREATE_MESSAGES)
    conn.execute(
        "INSERT INTO agent_messages "
        "(id, agent_id, direction, content, mode, status, created_at) "
        "VALUES ('old', 'agent', 'agent_to_user', 'preserved', "
        "'immediate', 'delivered', 1)"
    )
    conn.commit()
    conn.close()
    manager = AgentManager(str(path))
    message = manager.list_messages("agent")[0]
    assert message["content"] == "preserved"
    assert message["correlation"] == {}
    manager.close()
