"""Retrieval tool — search memory backends for relevant context."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec
from openjarvis.tools.storage._stubs import MemoryBackend
from openjarvis.tools.storage.context import format_context, trusted_results


@ToolRegistry.register("retrieval")
class RetrievalTool(BaseTool):
    """Search the memory backend and return formatted context."""

    tool_id = "retrieval"

    def __init__(
        self,
        backend: Optional[MemoryBackend] = None,
        *,
        top_k: int = 5,
    ) -> None:
        self._backend = backend
        self._top_k = top_k

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="retrieval",
            description=(
                "Search the knowledge base for relevant"
                " information. Returns context with sources."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query to find relevant information.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results to return (default: 5).",
                    },
                },
                "required": ["query"],
            },
            category="memory",
            required_capabilities=["memory:read"],
            evidence_kinds=["external"],
        )

    def execute(self, **params: Any) -> ToolResult:
        if self._backend is None:
            return ToolResult(
                tool_name="retrieval",
                content="No memory backend configured.",
                success=False,
            )
        query = params.get("query", "")
        if not query:
            return ToolResult(
                tool_name="retrieval",
                content="No query provided.",
                success=False,
            )
        top_k = params.get("top_k", self._top_k)
        try:
            results = self._backend.retrieve(query, top_k=top_k)
        except Exception as exc:
            return ToolResult(
                tool_name="retrieval",
                content=f"Retrieval error: {exc}",
                success=False,
            )
        results = trusted_results(results)
        if not results:
            return ToolResult(
                tool_name="retrieval",
                content="No relevant results found.",
                success=True,
                metadata={
                    "num_results": 0,
                    "evidence": {
                        "provider": "memory_retrieval",
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        "records": [],
                    },
                },
            )

        formatted = format_context(results)
        evidence_records = []
        for result in results:
            metadata = result.metadata if isinstance(result.metadata, dict) else {}
            evidence_records.append(
                {
                    "source": result.source or "memory",
                    "source_id": str(
                        metadata.get("doc_id")
                        or metadata.get("chunk_id")
                        or metadata.get("source_id")
                        or ""
                    ),
                    "title": str(metadata.get("title", "")),
                    "url": str(metadata.get("url", "")),
                    "content": result.content,
                    "metadata": {
                        "score": result.score,
                        "trust": str(metadata.get("trust", "")),
                        "timestamp": str(metadata.get("timestamp", "")),
                        "chunk_id": str(metadata.get("chunk_id", "")),
                    },
                }
            )

        return ToolResult(
            tool_name="retrieval",
            content=formatted,
            success=True,
            metadata={
                "num_results": len(results),
                "evidence": {
                    "provider": "memory_retrieval",
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "records": evidence_records,
                },
            },
        )


__all__ = ["RetrievalTool"]
