#!/usr/bin/env python3
"""Exercise the production evidence gate with Ollama and controlled fixtures.

Run from the repository: uv run python scripts/check_live_evidence.py
This is a live verifier check, not a tool-discovery or HTTP endpoint test.
No service restart, configuration changes, or memory writes are performed.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from types import SimpleNamespace

from openjarvis.agents._stubs import AgentResult
from openjarvis.core.config import load_config
from openjarvis.core.evidence import finalize_agent_result_with_evidence
from openjarvis.core.types import ToolResult
from openjarvis.engine.ollama import OllamaEngine
from openjarvis.tools._stubs import ToolSpec


@dataclass(frozen=True)
class Case:
    name: str
    answer: str
    sources: tuple[str, ...]
    expected: dict[str, str]
    success: bool = True


CASES = (
    Case("supported", "The launch is approved.",
         ("The launch is approved.",),
         {"evidence_status": "obtained", "grounding_status": "supported"}),
    Case("unsupported_relationship", "The launch is canceled.",
         ("The launch is approved.",),
         {"evidence_status": "obtained", "grounding_status": "unsupported"}),
    Case("unsupported_number", "The launch has 42 attendees.",
         ("The launch is approved.",),
         {"evidence_status": "obtained", "grounding_status": "unsupported",
          "grounding_method": "numeric_anchor"}),
    Case("conflicting_sources", "The launch is approved.",
         ("The launch is approved.", "The launch is canceled."),
         {"evidence_status": "conflicting", "evidence_conflict_status": "conflicting"}),
    Case("agreeing_sources", "The launch is approved.",
         ("The launch is approved.", "The launch has received approval."),
         {"evidence_status": "obtained", "evidence_conflict_status": "consistent",
          "grounding_status": "supported"}),
    Case("failed_tool", "The launch is approved.",
         ("The launch is approved.",),
         {"evidence_status": "required_not_obtained"}, success=False),
    Case("empty_retrieval", "The launch is approved.", (),
         {"evidence_status": "required_not_obtained"}),
)


def run_case(engine, model: str, case: Case) -> tuple[bool, str]:
    tool = SimpleNamespace(spec=ToolSpec(
        name="live_evidence_fixture", description="Controlled live verifier fixture",
        evidence_kinds=["current"],
    ))
    agent = SimpleNamespace(_engine=engine, _model=model, _tools=[tool])
    result = AgentResult(content=case.answer, tool_results=[ToolResult(
        tool_name=tool.spec.name, content="Controlled evidence fixture.",
        success=case.success,
        metadata={"evidence": {"records": [
            {"content": content, "url": f"https://source-{index}.test/status"}
            for index, content in enumerate(case.sources)
        ]}},
    )])
    result = finalize_agent_result_with_evidence(
        agent, "What is the latest launch status?", result,
    )
    mismatches = [
        f"{key}={result.metadata.get(key)!r} (expected {value!r})"
        for key, value in case.expected.items()
        if result.metadata.get(key) != value
    ]
    should_allow = case.expected.get("grounding_status") == "supported"
    if (result.content == case.answer) != should_allow:
        mismatches.append("final answer was incorrectly allowed or replaced")
    if mismatches:
        reason = result.metadata.get("grounding_reason", "")
        return False, "; ".join(mismatches) + (f"; {reason}" if reason else "")
    return True, ", ".join(f"{key}={value}" for key, value in case.expected.items())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Defaults to intelligence.default_model")
    parser.add_argument("--host", help="Defaults to engine.ollama.host")
    parser.add_argument("--timeout", type=float, default=180, help="Seconds per model call")
    args = parser.parse_args()
    config = load_config()
    model = args.model or config.intelligence.default_model
    if not model:
        parser.error("No configured model; specify --model")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    engine = OllamaEngine(host=args.host or config.engine.ollama.host, timeout=args.timeout)
    print(f"Live evidence gate: model={model}, cases={len(CASES)}", flush=True)
    failures = 0
    try:
        for case in CASES:
            print(f"RUN  {case.name}", flush=True)
            started = time.monotonic()
            try:
                passed, detail = run_case(engine, model, case)
            except Exception as exc:
                passed, detail = False, f"{type(exc).__name__}: {exc}"
            failures += not passed
            print(f"{'PASS' if passed else 'FAIL'} {case.name} "
                  f"({time.monotonic() - started:.1f}s): {detail}", flush=True)
    finally:
        engine.close()
    print(f"{len(CASES) - failures}/{len(CASES)} passed; {failures} failed", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
