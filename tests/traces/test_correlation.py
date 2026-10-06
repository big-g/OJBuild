"""Concurrent collectors and reused tool workers retain distinct trusted identities."""

import concurrent.futures
import threading

import pytest

from openjarvis.agents._stubs import AgentResult
from openjarvis.core.correlation import (
    ExecutionIdentity,
    current_identity,
    execution_scope,
)
from openjarvis.core.events import EventBus, EventType
from openjarvis.tools._stubs import _BoundedToolRunner
from openjarvis.traces.collector import TraceCollector
from openjarvis.traces.store import TraceStore


def test_scope_restores_identity_after_failure_and_payload_cannot_forge_events():
    bus = EventBus()
    identity = ExecutionIdentity(user_id="verified")
    before = current_identity()
    with pytest.raises(ValueError), execution_scope(identity):
        event = bus.publish(
            EventType.TOOL_CALL_START, {"correlation": {"user_id": "forged"}}
        )
        assert event.correlation["user_id"] == "verified"
        assert event.correlation["trace_id"] == identity.trace_id
        raise ValueError("failure")
    assert current_identity() is before


def test_parallel_collectors_do_not_capture_other_queries_or_unscoped_events():
    bus = EventBus()
    barrier = threading.Barrier(2)

    class Agent:
        agent_id = "test"

        def run(self, query, **kwargs):
            bus.publish(EventType.INFERENCE_START, {"model": query})
            barrier.wait(timeout=5)
            bus.publish(EventType.INFERENCE_END, {"content": query, "total_tokens": 1})
            with execution_scope(None):
                bus.publish(EventType.INFERENCE_END, {"content": "unrelated"})
            return AgentResult(content=query, turns=1)

    def run(query):
        collector = TraceCollector(Agent(), bus=bus)
        collector.run(query)
        return collector.last_trace

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        traces = list(executor.map(run, ["first", "second"]))
    assert len({trace.trace_id for trace in traces}) == 2
    for query, trace in zip(["first", "second"], traces):
        generated = [
            step.output["content"] for step in trace.steps if "tokens" in step.output
        ]
        assert generated == [query]
        assert trace.metadata["correlation"]["trace_id"] == trace.trace_id


def test_reused_tool_worker_copies_context_and_does_not_leak_previous_turn():
    runner = _BoundedToolRunner(max_workers=1, max_pending=2)
    bus = EventBus()
    try:
        identities = [
            ExecutionIdentity(user_id="first"),
            ExecutionIdentity(user_id="second"),
        ]
        for identity in identities:
            with execution_scope(identity):
                future = runner.submit(lambda: bus.publish(EventType.TOOL_CALL_END))
            assert future.result(timeout=5).correlation == identity.metadata()
        with execution_scope(None):
            future = runner.submit(lambda: bus.publish(EventType.TOOL_CALL_END))
        assert future.result(timeout=5).correlation == {}
    finally:
        runner.shutdown()


def test_failed_agent_trace_retains_identity_and_steps_without_exception_secrets(
    tmp_path,
):
    bus = EventBus()
    store = TraceStore(str(tmp_path / "traces.db"))
    identity = ExecutionIdentity(
        user_id="verified", session_id="session", conversation_id="session"
    )

    class Agent:
        agent_id = "failing"

        def run(self, query, **kwargs):
            bus.publish(EventType.INFERENCE_START, {"model": "test"})
            bus.publish(EventType.INFERENCE_END, {"content": "step"})
            raise ValueError("protected-secret")

    collector = TraceCollector(Agent(), bus=bus, store=store)
    with execution_scope(identity), pytest.raises(ValueError, match="protected-secret"):
        collector.run("query")
    trace = store.get(identity.trace_id)
    assert trace.outcome == "failure"
    assert trace.metadata["correlation"] == identity.metadata()
    assert trace.metadata["error_type"] == "ValueError"
    assert len(trace.steps) == 1
    assert "protected-secret" not in str(trace)
    assert all(not callbacks for callbacks in bus._subscribers.values())


def test_durable_routing_metadata_is_scoped_and_cannot_contaminate_old_trace(tmp_path):
    from openjarvis.core.routing_context import routing_scope
    from openjarvis.core.types import Trace

    store = TraceStore(str(tmp_path / "routing.db"))
    identity = ExecutionIdentity(user_id="verified")
    decision = {"model": "oj/server/model", "reason": "explicit", "fallback_used": True}
    with execution_scope(identity), routing_scope(decision):
        trace = Trace(trace_id=identity.trace_id, query="q", model="task/coding")
        store.save(trace)
        unrelated = Trace(query="old", model="legacy")
        store.save(unrelated)
    saved = store.get(identity.trace_id)
    assert saved.metadata["routing"] == decision
    assert saved.model == decision["model"]
    assert "routing" not in store.get(unrelated.trace_id).metadata
    store.close()
