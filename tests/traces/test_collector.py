"""Tests for the TraceCollector."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

from openjarvis.agents._stubs import AgentContext, AgentResult, BaseAgent
from openjarvis.core.evidence import (
    EvidenceAssessment,
    EvidenceKind,
    EvidenceRequirement,
    EvidenceStatus,
    apply_tool_evidence_to_result,
)
from openjarvis.core.events import EventBus, EventType
from openjarvis.core.types import StepType, ToolResult
from openjarvis.traces.collector import TraceCollector
from openjarvis.traces.store import TraceStore


class _FakeAgent(BaseAgent):
    """Minimal agent that returns a fixed response."""

    agent_id = "fake"

    def __init__(
        self,
        response: str = "test response",
        bus: Optional[EventBus] = None,
    ) -> None:
        self._response = response
        self._bus = bus

    def run(
        self,
        input: str,
        context: Optional[AgentContext] = None,
        **kwargs: Any,
    ) -> AgentResult:
        # Simulate an inference step via event bus
        if self._bus:
            self._bus.publish(
                EventType.INFERENCE_START,
                {
                    "model": "qwen3:8b",
                    "engine": "ollama",
                },
            )
            self._bus.publish(
                EventType.INFERENCE_END,
                {
                    "total_tokens": 50,
                },
            )
        return AgentResult(content=self._response, turns=1)


class _EvidenceAgent(BaseAgent):
    """Agent that already carries a completed evidence verdict."""

    agent_id = "evidence"

    def __init__(self) -> None:
        super().__init__(MagicMock(), "test-model")

    def run(
        self,
        input: str,
        context: Optional[AgentContext] = None,
        **kwargs: Any,
    ) -> AgentResult:
        return AgentResult(
            content="grounded answer",
            turns=1,
            metadata={
                "evidence_required": True,
                "evidence_kind": "external",
                "evidence_status": "obtained",
                "evidence_reason": "External retrieval required.",
                "evidence_records": 2,
            },
        )


class _ToolAgent(BaseAgent):
    """Agent that simulates a tool call during execution."""

    agent_id = "tool_agent"

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    def run(
        self,
        input: str,
        context: Optional[AgentContext] = None,
        **kwargs: Any,
    ) -> AgentResult:
        # Simulate inference + tool call + inference
        inf = {"model": "qwen3:8b", "engine": "ollama"}
        self._bus.publish(EventType.INFERENCE_START, inf)
        self._bus.publish(EventType.INFERENCE_END, {"total_tokens": 30})
        self._bus.publish(
            EventType.TOOL_CALL_START,
            {
                "tool": "calculator",
                "arguments": {"expr": "2+2"},
            },
        )
        self._bus.publish(
            EventType.TOOL_CALL_END,
            {
                "tool": "calculator",
                "success": True,
                "latency": 0.01,
            },
        )
        self._bus.publish(EventType.INFERENCE_START, inf)
        self._bus.publish(EventType.INFERENCE_END, {"total_tokens": 20})
        return AgentResult(content="4", turns=2)


class _EvidenceToolEventAgent(BaseAgent):
    """Agent that emits one evidence-bearing tool event."""

    agent_id = "evidence_tool_event"

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    def run(
        self,
        input: str,
        context: Optional[AgentContext] = None,
        **kwargs: Any,
    ) -> AgentResult:
        self._bus.publish(
            EventType.TOOL_CALL_START,
            {
                "tool": "knowledge_search",
                "arguments": {"query": "launch plan"},
            },
        )
        self._bus.publish(
            EventType.TOOL_CALL_END,
            {
                "tool": "knowledge_search",
                "success": True,
                "latency": 0.01,
                "result": "Launch is Friday.",
                "metadata": {
                    "evidence": {
                        "provider": "knowledge_search",
                        "records": [
                            {
                                "source": "obsidian",
                                "source_id": "doc-1",
                                "content": "Launch is Friday.",
                            }
                        ],
                    }
                },
            },
        )
        return AgentResult(content="Launch is Friday.", turns=1)


class _GroundingTraceTool:
    tool_id = "grounding_trace"

    @property
    def spec(self):
        from openjarvis.tools._stubs import ToolSpec

        return ToolSpec(
            name="grounding_trace",
            description="Grounding trace test tool.",
            evidence_kinds=["current"],
        )


class _GroundingTraceAgent:
    agent_id = "grounding_trace_agent"

    def run(self, input, context=None, **kwargs):
        return AgentResult(
            content="Tomorrow's high will be 82°F and it will be sunny.",
            turns=1,
            tool_results=[
                ToolResult(
                    tool_name="grounding_trace",
                    content="Forecast: tomorrow high 82°F.",
                    success=True,
                    metadata={
                        "evidence": {
                            "provider": "test",
                            "records": [
                                {
                                    "source": "weather",
                                    "content": "Forecast: tomorrow high 82°F.",
                                }
                            ],
                        }
                    },
                )
            ],
        )


class _GroundingTraceEngine:
    def generate(self, messages, *, model, temperature, max_tokens, **kwargs):
        return {
            "content": (
                '{"supported": false, '
                '"unsupported_claims": ["Sunny conditions are not in evidence."], '
                '"reason": "The condition is unsupported."}'
            )
        }


class TestTraceCollector:
    def test_basic_collection(self, tmp_path: Path) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _FakeAgent(response="hello", bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        result = collector.run("say hello")

        assert result.content == "hello"
        assert store.count() == 1

        traces = store.list_traces()
        trace = traces[0]
        assert trace.query == "say hello"
        assert trace.agent == "fake"
        assert trace.model == "qwen3:8b"
        assert trace.engine == "ollama"
        assert trace.result == "hello"
        store.close()

    def test_initial_trace_captures_agent_evidence_verdict(
        self,
        tmp_path: Path,
    ) -> None:
        store = TraceStore(tmp_path / "test.db")
        collector = TraceCollector(_EvidenceAgent(), store=store)

        collector.run("search for this")

        trace = store.list_traces()[0]
        assert trace.metadata["evidence"] == {
            "required": True,
            "kind": "external",
            "status": "obtained",
            "reason": "External retrieval required.",
            "records": 2,
        }
        store.close()

    def test_post_run_evidence_annotation_persists(
        self,
        tmp_path: Path,
    ) -> None:
        store = TraceStore(tmp_path / "test.db")
        collector = TraceCollector(_FakeAgent(), store=store)

        collector.run("What's the weather tomorrow?")
        requirement = EvidenceRequirement(
            required=True,
            kind=EvidenceKind.CURRENT,
            reason="Current data required.",
        )
        assessment = EvidenceAssessment(
            status=EvidenceStatus.REQUIRED_NOT_OBTAINED,
            reason="Required evidence was not obtained.",
        )

        blocked = "I couldn't retrieve the required data."
        assert (
            collector.annotate_evidence(
                requirement,
                assessment,
                final_content=blocked,
            )
            is True
        )

        trace = store.list_traces()[0]
        assert trace.metadata["evidence"] == {
            "required": True,
            "kind": "current",
            "status": "required_not_obtained",
            "reason": "Required evidence was not obtained.",
            "records": 0,
        }
        assert trace.result == blocked
        respond_steps = [
            step
            for step in trace.steps
            if step.step_type == StepType.RESPOND
        ]
        assert respond_steps[-1].output["content"] == blocked
        store.close()

    def test_tool_step_preserves_evidence_provenance(
        self,
        tmp_path: Path,
    ) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        collector = TraceCollector(
            _EvidenceToolEventAgent(bus),
            store=store,
            bus=bus,
        )

        collector.run("Search my notes for the launch plan.")

        trace = store.list_traces()[0]
        tool_steps = [
            step
            for step in trace.steps
            if step.step_type == StepType.TOOL_CALL
        ]
        assert len(tool_steps) == 1
        evidence = tool_steps[0].metadata["evidence"]
        assert evidence["provider"] == "knowledge_search"
        assert evidence["records"][0]["source"] == "obsidian"
        assert evidence["records"][0]["source_id"] == "doc-1"
        store.close()

    def test_result_processor_finalizes_before_trace_complete(
        self,
        tmp_path: Path,
    ) -> None:
        bus = EventBus(record_history=True)
        store = TraceStore(tmp_path / "test.db")
        requirement = EvidenceRequirement(
            required=True,
            kind=EvidenceKind.CURRENT,
            reason="Current data required.",
        )

        def finalize(result: AgentResult) -> AgentResult:
            apply_tool_evidence_to_result(requirement, [], result)
            return result

        collector = TraceCollector(
            _FakeAgent(response="Unsupported current answer.", bus=bus),
            store=store,
            bus=bus,
            result_processor=finalize,
        )

        result = collector.run("What's the weather tomorrow?")

        blocked = "I couldn't retrieve the required data."
        assert result.content == blocked
        trace = store.list_traces()[0]
        assert trace.result == blocked
        assert trace.metadata["evidence"]["status"] == "required_not_obtained"

        trace_events = [
            event
            for event in bus.history
            if event.event_type == EventType.TRACE_COMPLETE
        ]
        assert len(trace_events) == 1
        assert trace_events[0].data["trace"].result == blocked
        assert (
            trace_events[0].data["trace"].metadata["evidence"]["status"]
            == "required_not_obtained"
        )
        store.close()

    def test_trace_records_semantic_grounding_verdict(
        self,
        tmp_path: Path,
    ) -> None:
        from openjarvis.core.evidence import apply_tool_evidence_to_result

        store = TraceStore(tmp_path / "grounding.db")
        requirement = EvidenceRequirement(
            required=True,
            kind=EvidenceKind.CURRENT,
            reason="Current data required.",
        )
        tool = _GroundingTraceTool()

        def finalize(result: AgentResult) -> AgentResult:
            apply_tool_evidence_to_result(
                requirement,
                [tool],
                result,
                query="What's the weather tomorrow?",
                engine=_GroundingTraceEngine(),
                model="test-model",
                validate_grounding=True,
            )
            return result

        collector = TraceCollector(
            _GroundingTraceAgent(),
            store=store,
            result_processor=finalize,
        )

        result = collector.run("What's the weather tomorrow?")

        blocked = "I couldn't verify the response against the retrieved evidence."
        assert result.content == blocked
        trace = store.list_traces()[0]
        assert trace.result == blocked
        assert trace.metadata["evidence"]["status"] == "obtained"
        assert trace.metadata["evidence"]["grounding"] == {
            "status": "unsupported",
            "reason": "The condition is unsupported.",
            "method": "llm_judge",
            "unsupported_claims": [
                "Sunny conditions are not in evidence."
            ],
        }
        store.close()

    def test_records_generate_steps(self, tmp_path: Path) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _FakeAgent(bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        collector.run("test")

        trace = store.list_traces()[0]
        generate_steps = [s for s in trace.steps if s.step_type == StepType.GENERATE]
        assert len(generate_steps) == 1
        assert generate_steps[0].output.get("tokens") == 50
        store.close()

    def test_records_tool_steps(self, tmp_path: Path) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _ToolAgent(bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        collector.run("What is 2+2?")

        trace = store.list_traces()[0]
        tool_steps = [s for s in trace.steps if s.step_type == StepType.TOOL_CALL]
        assert len(tool_steps) == 1
        assert tool_steps[0].input["tool"] == "calculator"
        assert tool_steps[0].output["success"] is True
        store.close()

    def test_records_respond_step(self, tmp_path: Path) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _FakeAgent(response="final answer", bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        collector.run("test")

        trace = store.list_traces()[0]
        respond_steps = [s for s in trace.steps if s.step_type == StepType.RESPOND]
        assert len(respond_steps) == 1
        assert respond_steps[0].output["content"] == "final answer"
        store.close()

    def test_records_memory_retrieve(self, tmp_path: Path) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _FakeAgent(bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        # Monkey-patch agent to emit memory event
        original_run = agent.run

        def run_with_memory(input, context=None, **kwargs):
            bus.publish(
                EventType.MEMORY_RETRIEVE,
                {
                    "query": "meeting notes",
                    "num_results": 3,
                    "latency": 0.2,
                },
            )
            return original_run(input, context=context, **kwargs)

        agent.run = run_with_memory
        collector.run("find my meeting notes")

        trace = store.list_traces()[0]
        retrieve_steps = [s for s in trace.steps if s.step_type == StepType.RETRIEVE]
        assert len(retrieve_steps) == 1
        assert retrieve_steps[0].input["query"] == "meeting notes"
        store.close()

    def test_publishes_trace_complete(self, tmp_path: Path) -> None:
        bus = EventBus(record_history=True)
        store = TraceStore(tmp_path / "test.db")
        agent = _FakeAgent(bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        collector.run("test")

        trace_events = [
            e for e in bus.history if e.event_type == EventType.TRACE_COMPLETE
        ]
        assert len(trace_events) == 1
        assert trace_events[0].data["trace"].query == "test"
        store.close()

    def test_no_store(self) -> None:
        """Collector works without a store (just collects, doesn't persist)."""
        bus = EventBus()
        agent = _FakeAgent(response="ok", bus=bus)
        collector = TraceCollector(agent, bus=bus)  # no store

        result = collector.run("test")
        assert result.content == "ok"

    def test_no_bus(self, tmp_path: Path) -> None:
        """Collector works without a bus (no event-based step collection)."""
        store = TraceStore(tmp_path / "test.db")
        agent = _FakeAgent(response="ok")
        collector = TraceCollector(agent, store=store)  # no bus

        result = collector.run("test")
        assert result.content == "ok"
        assert store.count() == 1
        # Only the RESPOND step (no events to capture)
        trace = store.list_traces()[0]
        assert len(trace.steps) == 1
        assert trace.steps[0].step_type == StepType.RESPOND
        store.close()

    def test_timing(self, tmp_path: Path) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _FakeAgent(bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        before = time.time()
        collector.run("test")
        after = time.time()

        trace = store.list_traces()[0]
        assert trace.started_at >= before
        assert trace.ended_at <= after
        assert trace.ended_at >= trace.started_at
        store.close()

    def test_unsubscribes_after_run(self, tmp_path: Path) -> None:
        """Events after run() completes should NOT affect the next trace."""
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _FakeAgent(bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        collector.run("first")

        # Emit events after run — should not affect stored trace
        bus.publish(EventType.INFERENCE_START, {"model": "stray"})
        bus.publish(EventType.INFERENCE_END, {"total_tokens": 999})

        assert store.count() == 1
        trace = store.list_traces()[0]
        # No step with model="stray"
        for s in trace.steps:
            assert s.input.get("model") != "stray"
        store.close()


class _RichToolAgent(BaseAgent):
    """Agent that emits content-enriched events for testing."""

    agent_id = "rich_tool_agent"

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    def run(
        self,
        input: str,
        context: Optional[AgentContext] = None,
        **kwargs: Any,
    ) -> AgentResult:
        from openjarvis.core.types import ToolResult

        # Turn 1: inference with tool call request
        self._bus.publish(
            EventType.INFERENCE_START,
            {
                "model": "test-model",
                "engine": "test",
            },
        )
        self._bus.publish(
            EventType.INFERENCE_END,
            {
                "total_tokens": 30,
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
                "content": "I'll calculate that for you.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "calculator",
                        "arguments": '{"expr": "2+2"}',
                    },
                ],
                "finish_reason": "tool_calls",
            },
        )
        # Tool execution
        self._bus.publish(
            EventType.TOOL_CALL_START,
            {
                "tool": "calculator",
                "arguments": {"expr": "2+2"},
            },
        )
        self._bus.publish(
            EventType.TOOL_CALL_END,
            {
                "tool": "calculator",
                "success": True,
                "latency": 0.01,
                "result": "4",
            },
        )
        # Turn 2: final answer
        self._bus.publish(
            EventType.INFERENCE_START,
            {
                "model": "test-model",
                "engine": "test",
            },
        )
        self._bus.publish(
            EventType.INFERENCE_END,
            {
                "total_tokens": 15,
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "content": "The answer is 4.",
                "tool_calls": [],
                "finish_reason": "stop",
            },
        )

        # Return result with messages in metadata
        messages = [
            {"role": "user", "content": input},
            {"role": "assistant", "content": "I'll calculate that for you."},
            {"role": "tool", "content": "4", "name": "calculator"},
            {"role": "assistant", "content": "The answer is 4."},
        ]
        return AgentResult(
            content="The answer is 4.",
            tool_results=[
                ToolResult(tool_name="calculator", content="4", success=True),
            ],
            turns=2,
            metadata={"messages": messages},
        )


class TestRichTraceCollector:
    def test_captures_content_in_generate_steps(self, tmp_path: Path) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _RichToolAgent(bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        collector.run("What is 2+2?")

        trace = store.list_traces()[0]
        gen_steps = [s for s in trace.steps if s.step_type == StepType.GENERATE]
        assert len(gen_steps) == 2
        assert gen_steps[0].output["content"] == "I'll calculate that for you."
        expected_tc = [
            {"id": "call_1", "name": "calculator", "arguments": '{"expr": "2+2"}'},
        ]
        assert gen_steps[0].output["tool_calls"] == expected_tc
        assert gen_steps[0].output["finish_reason"] == "tool_calls"
        assert gen_steps[1].output["content"] == "The answer is 4."
        assert gen_steps[1].output["finish_reason"] == "stop"
        store.close()

    def test_captures_tool_arguments_and_result(self, tmp_path: Path) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _RichToolAgent(bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        collector.run("What is 2+2?")

        trace = store.list_traces()[0]
        tool_steps = [s for s in trace.steps if s.step_type == StepType.TOOL_CALL]
        assert len(tool_steps) == 1
        assert tool_steps[0].input["tool"] == "calculator"
        assert tool_steps[0].input["arguments"] == {"expr": "2+2"}
        assert tool_steps[0].output["result"] == "4"
        assert tool_steps[0].output["success"] is True
        store.close()

    def test_captures_messages_in_trace(self, tmp_path: Path) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _RichToolAgent(bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        collector.run("What is 2+2?")

        trace = store.list_traces()[0]
        assert len(trace.messages) == 4
        assert trace.messages[0]["role"] == "user"
        assert trace.messages[3]["role"] == "assistant"
        store.close()

    def test_last_trace_property(self, tmp_path: Path) -> None:
        bus = EventBus()
        store = TraceStore(tmp_path / "test.db")
        agent = _RichToolAgent(bus=bus)
        collector = TraceCollector(agent, store=store, bus=bus)

        collector.run("What is 2+2?")

        trace = collector.last_trace
        assert trace is not None
        assert trace.query == "What is 2+2?"
        assert len(trace.messages) == 4
        store.close()
