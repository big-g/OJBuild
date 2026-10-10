#!/usr/bin/env python3
"""Read-only keyless search transport probe; does not test HTTP tool governance.

Run from the repository: uv run python scripts/check_web_search.py
No model inference, service restart, configuration changes or memory writes.
"""

import argparse
import json
import time

from openjarvis.core.config import load_config
from openjarvis.core.evidence import assess_tool_results, detect_evidence_requirement
from openjarvis.tools.web_search import WebSearchTool


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="Kernersville NC weather forecast")
    parser.add_argument("--snippets-only", action="store_true")
    args = parser.parse_args()
    config = load_config()
    selected = config.tools.enabled or config.agent.tools
    if isinstance(selected, str):
        selected = [name.strip() for name in selected.split(",") if name.strip()]
    print("Configured server agent:", config.server.agent or "none")
    print("Configured tools:", selected or "server defaults (includes web_search)")
    print("Transport-only probe: Tavily disabled; no weather API key used.")
    tool = WebSearchTool()
    tool._api_key = None
    started = time.monotonic()
    result = tool.execute(query=args.query, fetch_pages=not args.snippets_only)
    assessment = assess_tool_results(
        detect_evidence_requirement(args.query), [tool], [result]
    )
    print(
        json.dumps(
            {
                "success": result.success,
                "elapsed_seconds": round(time.monotonic() - started, 1),
                "provider": result.metadata.get("engine"),
                "attempts": result.metadata.get("attempts", []),
                "evidence_status": assessment.status.value,
                "sources": [
                    {
                        "url": record["url"],
                        "content_scope": record.get("content_scope", "search_snippet"),
                        "characters": len(record["content"]),
                    }
                    for record in result.metadata.get("results", [])
                ],
            },
            indent=2,
        )
    )
    print("Retrieved text (first 6000 characters):\n" + result.content[:6000])
    print("Search evidence is not proof of forecast relevance or answer grounding.")
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
