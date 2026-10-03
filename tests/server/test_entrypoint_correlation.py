"""Offline regressions for channel, scheduled and manual operator identities."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from openjarvis.agents._stubs import AgentResult
from openjarvis.core.config import JarvisConfig
from openjarvis.core.correlation import (
    ExecutionIdentity,
    current_identity,
    execution_scope,
)
from openjarvis.core.events import EventBus, EventType
from openjarvis.operators.manager import OperatorManager
from openjarvis.operators.types import OperatorManifest
from openjarvis.scheduler.scheduler import TaskScheduler
from openjarvis.scheduler.store import SchedulerStore
from openjarvis.server.channel_bridge import ChannelBridge
from openjarvis.server.session_store import SessionStore
from openjarvis.system.orchestrator import QueryOrchestrator
from openjarvis.traces.collector import TraceCollector
from openjarvis.traces.store import TraceStore


def test_channel_turn_messages_events_and_direct_trace_share_identity(tmp_path):
    sessions = SessionStore(str(tmp_path / "sessions.db"))
    traces = TraceStore(tmp_path / "traces.db")
    bus = EventBus(record_history=True)
    observed = []

    def generate(*args, **kwargs):
        observed.append(current_identity().metadata())
        bus.publish(EventType.INFERENCE_END, {})
        return {"content": "OK"}

    config = JarvisConfig()
    config.agent.context_from_memory = False
    system = SimpleNamespace(
        config=config,
        engine=SimpleNamespace(generate=generate),
        model="test",
        engine_key="test",
        agent_name="none",
        trace_store=traces,
    )
    system.ask = QueryOrchestrator(system).ask
    bridge = ChannelBridge({}, sessions, bus, system=system)
    ambient = ExecutionIdentity(user_id="request-user")
    with execution_scope(ambient):
        for _ in range(2):
            assert (
                bridge.handle_incoming(
                    "sender",
                    "Hello",
                    "sms",
                    metadata={"user_id": "forged", "session_id": "forged"},
                )
                == "OK"
            )
            assert current_identity() is ambient
    session = sessions.get_or_create("sender", "sms")
    assert len(session["conversation_history"]) == 4
    assert observed[0]["turn_id"] != observed[1]["turn_id"]
    assert (
        observed[0]["session_id"] == observed[1]["session_id"] == session["session_id"]
    )
    for i, identity in enumerate(observed):
        assert identity["user_id"] == ""
        assert identity["conversation_id"] == session["session_id"]
        for message in session["conversation_history"][i * 2 : i * 2 + 2]:
            assert message["metadata"]["correlation"] == identity
        assert traces.get(identity["trace_id"]).metadata["correlation"] == identity
        assert bus.history[i].correlation == identity
    sessions.close()
    reopened = SessionStore(str(tmp_path / "sessions.db"))
    assert (
        reopened.get_or_create("sender", "sms")["session_id"] == session["session_id"]
    )
    assert (
        reopened.get_or_create("sender", "other")["session_id"] != session["session_id"]
    )
    reopened.close()
    traces.close()


@pytest.mark.parametrize("fail", [False, True])
def test_channel_research_summary_and_scope_cleanup(tmp_path, fail):
    sessions = SessionStore(":memory:")
    traces = TraceStore(tmp_path / "traces.db")
    observed = []

    def run(query):
        observed.append(current_identity().metadata())
        if fail:
            raise RuntimeError("private upstream detail")
        return AgentResult(content="Hello")

    agent = SimpleNamespace(run=run, _model="test", _tools=[])
    bridge = ChannelBridge(
        {}, sessions, EventBus(), deep_research_agent=agent, trace_store=traces
    )
    bridge.handle_incoming("sender", "Hi", "sms")
    identity = observed[0]
    trace = traces.get(identity["trace_id"])
    assert trace.metadata["correlation"] == identity
    if fail:
        assert trace.outcome == "failure"
        assert trace.metadata["error_type"] == "RuntimeError"
        assert "private upstream detail" not in str(trace)
    else:
        assert trace.result == "Hello"
    assert current_identity() is None
    sessions.close()
    traces.close()


def test_parallel_channel_sessions_and_command_failure_restore_identity():
    sessions = SessionStore(":memory:")
    observed = []

    def ask(query):
        observed.append(current_identity().metadata())
        return {"content": "OK"}

    bridge = ChannelBridge({}, sessions, EventBus(), system=SimpleNamespace(ask=ask))
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(
            workers.map(
                lambda sender: bridge.handle_incoming(sender, "Hi", "sms"),
                ["one", "two"],
            )
        )
    assert len({i["trace_id"] for i in observed}) == 2
    assert len({i["session_id"] for i in observed}) == 2
    ambient = ExecutionIdentity()
    with execution_scope(ambient):
        bridge.handle_incoming("one", "/help", "sms")
        assert current_identity() is ambient
        with pytest.raises(AttributeError):
            bridge.handle_incoming("one", None, "sms")
        assert current_identity() is ambient
    sessions.close()


@pytest.mark.parametrize("fail", [False, True])
def test_scheduled_operator_run_events_logs_and_agent_trace_correlate(tmp_path, fail):
    logs = SchedulerStore(tmp_path / "scheduler.db")
    traces = TraceStore(tmp_path / "traces.db")
    bus = EventBus(record_history=True)
    observed = []

    class Agent:
        agent_id = "operative"

        def run(self, query, context=None):
            observed.append(current_identity().metadata())
            bus.publish(EventType.INFERENCE_END, {})
            if fail:
                raise RuntimeError("engine down")
            return AgentResult(content="OK")

    def ask(query, **kwargs):
        result = TraceCollector(Agent(), bus=bus, store=traces).run(query)
        # Real JarvisSystem.ask returns a dict, which must be normalized for SQLite.
        return {"content": result.content, "usage": {}}

    scheduler = TaskScheduler(logs, system=SimpleNamespace(ask=ask), bus=bus)
    task = scheduler.create_task(
        "Hi",
        "interval",
        "60",
        metadata={
            "operator_id": "test",
            "user_id": "forged",
            "correlation": {"trace_id": "forged"},
        },
    )
    ambient = ExecutionIdentity(user_id="creator")
    with execution_scope(ambient):
        for _ in range(2):
            scheduler._execute_task(task)
            assert current_identity() is ambient
    assert observed[0]["trace_id"] != observed[1]["trace_id"]
    runs = logs.get_run_logs(task.id)
    for identity in observed:
        assert identity["user_id"] == identity["session_id"] == ""
        run_log = next(r for r in runs if r["correlation"] == identity)
        assert run_log["success"] == int(not fail)
        if not fail:
            assert run_log["result"] == "OK"
        assert traces.get(identity["trace_id"]).metadata["correlation"] == identity
        events = [e for e in bus.history if e.correlation == identity]
        assert events[0].event_type == EventType.SCHEDULER_TASK_START
        assert events[-1].event_type == EventType.SCHEDULER_TASK_END
    logs.close()
    reopened = SchedulerStore(tmp_path / "scheduler.db")
    assert reopened.get_run_logs(task.id) == runs
    reopened.close()
    traces.close()


def test_manual_operator_inherits_verified_scope_or_creates_standalone_identity():
    observed = []

    def ask(*args, **kwargs):
        observed.append(current_identity())
        if len(observed) == 3:
            raise RuntimeError("failure")
        return {"content": "OK"}

    manager = OperatorManager(SimpleNamespace(ask=ask))
    manager.register(OperatorManifest(id="test", name="Test"))
    assert manager.run_once("test") == "OK"
    ambient = ExecutionIdentity(user_id="verified", session_id="owned")
    with execution_scope(ambient):
        assert manager.run_once("test") == "OK"
        with pytest.raises(RuntimeError):
            manager.run_once("test")
        assert current_identity() is ambient
    assert observed[0].user_id == ""
    assert observed[1] is observed[2] is ambient
    assert current_identity() is None


def test_channel_schema_migration_preserves_legacy_history_and_identity(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE channel_sessions (
        sender_id TEXT, channel_type TEXT, conversation_history TEXT DEFAULT '[]',
        preferred_notification_channel TEXT, pending_response TEXT,
        created_at TEXT, updated_at TEXT, PRIMARY KEY(sender_id, channel_type))""")
    conn.execute(
        "INSERT INTO channel_sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "sender",
            "sms",
            '[{"role":"user","content":"old"}]',
            "sms",
            "pending",
            "old",
            "old",
        ),
    )
    conn.commit()
    conn.close()
    store = SessionStore(str(path))
    session = store.get_or_create("sender", "sms")
    assert session["session_id"]
    assert session["conversation_history"] == [{"role": "user", "content": "old"}]
    assert session["pending_response"] == "pending"
    assert session["preferred_notification_channel"] == "sms"
    store.close()
    store = SessionStore(str(path))
    assert store.get_or_create("sender", "sms") == session
    store.close()


def test_scheduler_schema_migration_keeps_legacy_logs(tmp_path):
    from openjarvis.scheduler.store import _CREATE_LOGS_TABLE

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(_CREATE_LOGS_TABLE)
    conn.execute(
        "INSERT INTO task_run_logs(task_id, started_at, result) VALUES(?, ?, ?)",
        ("old", "old", "preserved"),
    )
    conn.commit()
    conn.close()
    store = SchedulerStore(path)
    assert store.get_run_logs("old")[0]["result"] == "preserved"
    assert store.get_run_logs("old")[0]["correlation"] == {}
    store.close()


def test_parallel_scheduled_runs_keep_events_and_logs_separate(tmp_path):
    import threading

    logs = SchedulerStore(tmp_path / "scheduler.db")
    bus = EventBus(record_history=True)
    barrier = threading.Barrier(2)
    observed = {}

    def ask(query, **kwargs):
        identity = current_identity().metadata()
        observed[query] = identity
        barrier.wait(timeout=2)
        bus.publish(EventType.INFERENCE_END, {"query": query})
        assert current_identity().metadata() == identity
        return {"content": query}

    scheduler = TaskScheduler(logs, system=SimpleNamespace(ask=ask), bus=bus)
    tasks = [scheduler.create_task(q, "interval", "60") for q in ["one", "two"]]

    def execute(task):
        scheduler._execute_task(task)
        assert current_identity() is None

    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(execute, tasks))
    assert observed["one"]["trace_id"] != observed["two"]["trace_id"]
    for task in tasks:
        identity = observed[task.prompt]
        assert logs.get_run_logs(task.id)[0]["correlation"] == identity
        for event in bus.history:
            if event.data.get("task_id") == task.id:
                assert event.correlation == identity
    logs.close()
