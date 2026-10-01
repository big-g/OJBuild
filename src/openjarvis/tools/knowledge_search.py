"""KnowledgeSearchTool — bounded multi-query retrieval with source attribution.

Wraps ``KnowledgeStore`` so agents can search ingested documents by text query
and optional provenance filters (source, doc_type, author, date range).
Optionally delegates to a ``TwoStageRetriever`` for BM25 + reranking.
Additional focused queries use rank fusion with chunk-level deduplication.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from openjarvis.connectors.store import KnowledgeStore
from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec
from openjarvis.tools.storage._stubs import RetrievalResult
from openjarvis.tools.storage.context import trusted_results

if TYPE_CHECKING:
    from openjarvis.connectors.retriever import TwoStageRetriever


@ToolRegistry.register("knowledge_search")
class KnowledgeSearchTool(BaseTool):
    """Search the knowledge store using filtered BM25 retrieval.

    Results include source attribution so agents can cite provenance.
    When a ``TwoStageRetriever`` is supplied it is used in place of the
    store's direct ``retrieve`` method, enabling optional semantic reranking.
    """

    tool_id = "knowledge_search"

    def __init__(
        self,
        store: Optional[KnowledgeStore] = None,
        retriever: Optional["TwoStageRetriever"] = None,
    ) -> None:
        self._store = store
        self._retriever = retriever

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="knowledge_search",
            description=(
                "Search ingested personal knowledge (emails, Slack messages,"
                " documents) using full-text BM25 retrieval with optional"
                " filters for source, type, author, and date range. Supply"
                " queries to combine focused searches for a complex question."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Full-text search query.",
                        "minLength": 1,
                        "maxLength": 2000,
                    },
                    "queries": {
                        "type": "array",
                        "maxItems": 4,
                        "items": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 2000,
                        },
                        "description": (
                            "Up to four additional focused keyword queries."
                            " Results are merged without duplicate chunks;"
                            " the same filters apply to every query."
                        ),
                    },
                    "source": {
                        "type": "string",
                        "description": (
                            "Filter by source connector"
                            " (e.g. 'gmail', 'slack', 'obsidian')."
                        ),
                    },
                    "doc_type": {
                        "type": "string",
                        "description": (
                            "Filter by document type"
                            " (e.g. 'email', 'message', 'document')."
                        ),
                    },
                    "author": {
                        "type": "string",
                        "description": "Filter by author.",
                    },
                    "since": {
                        "type": "string",
                        "description": (
                            "Exclude documents before this ISO 8601 timestamp."
                        ),
                    },
                    "until": {
                        "type": "string",
                        "description": (
                            "Exclude documents after this ISO 8601 timestamp."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": (
                            "Maximum total merged results (default 10, maximum 50)."
                        ),
                    },
                },
                "required": ["query"],
            },
            category="knowledge",
            required_capabilities=["memory:read"],
            evidence_kinds=["external"],
        )

    def execute(self, **params: Any) -> ToolResult:
        if self._store is None and self._retriever is None:
            return ToolResult(
                tool_name="knowledge_search",
                content="No knowledge store configured.",
                success=False,
            )

        query: str = params.get("query", "")
        if not isinstance(query, str) or not query.strip():
            return ToolResult(
                tool_name="knowledge_search",
                content="No query provided.",
                success=False,
            )

        additional = params.get("queries", [])
        if (
            not isinstance(additional, list)
            or len(additional) > 4
            or any(not isinstance(q, str) or not q.strip() for q in additional)
            or any(len(q) > 2000 for q in [query, *additional])
        ):
            return ToolResult(
                tool_name="knowledge_search",
                success=False,
                content=(
                    "Provide up to four nonempty queries, each at most 2000 characters."
                ),
            )
        queries = list(dict.fromkeys(q.strip() for q in [query, *additional]))
        raw_top_k = params.get("top_k", 10)
        try:
            top_k = int(raw_top_k)
            if isinstance(raw_top_k, bool) or str(raw_top_k) != str(top_k):
                raise ValueError
            if not 1 <= top_k <= 50:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            return ToolResult(
                tool_name="knowledge_search",
                success=False,
                content="top_k must be an integer between 1 and 50.",
            )
        source: Optional[str] = params.get("source")
        doc_type: Optional[str] = params.get("doc_type")
        author: Optional[str] = params.get("author")
        since: Optional[str] = params.get("since")
        until: Optional[str] = params.get("until")

        backend = self._retriever or self._store
        filters = {
            k: v
            for k, v in {
                "source": source,
                "doc_type": doc_type,
                "author": author,
                "since": since,
                "until": until,
            }.items()
            if v
        }
        batches = []
        for focused_query in queries:
            try:
                batch = backend.retrieve(  # type: ignore[union-attr]
                    focused_query, top_k=top_k, **filters
                )
                batches.append(trusted_results(batch))
            except Exception:
                # A failed subquery must not masquerade as complete evidence.
                return ToolResult(
                    tool_name="knowledge_search",
                    success=False,
                    content=(
                        "Knowledge retrieval failed; "
                        "no complete result set is available."
                    ),
                    metadata={"failed_query": focused_query, "queries": queries},
                )
        results = _merge_query_results(queries, batches, top_k)

        if not results:
            return ToolResult(
                tool_name="knowledge_search",
                content="No relevant results found.",
                success=True,
                metadata={
                    "num_results": 0,
                    "queries": queries,
                    "evidence": {
                        "provider": "knowledge_search",
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        "records": [],
                    },
                },
            )

        lines: list[str] = []
        evidence_records: list[dict[str, Any]] = []
        for i, result in enumerate(results, start=1):
            meta = result.metadata if isinstance(result.metadata, dict) else {}
            src_label = result.source or meta.get("source", "")
            title = meta.get("title", "")
            result_author = meta.get("author", "")
            url = meta.get("url", "")

            # Build header line
            header_parts: list[str] = []
            if src_label:
                header_parts.append(f"[{src_label}]")
            if title:
                header_parts.append(title)
            if result_author:
                header_parts.append(f"by {result_author}")
            for key in ("section", "jurisdiction", "version"):
                if meta.get(key):
                    header_parts.append(f"{key}: {meta[key]}")
            if url:
                header_parts.append(f"({url})")

            header = " ".join(header_parts) if header_parts else "(unknown source)"
            lines.append(f"**Result {i}:** {header}")
            lines.append(result.content)
            lines.append("")

            evidence_records.append(
                {
                    "source": src_label or "knowledge_store",
                    "source_id": str(
                        meta.get("doc_id")
                        or meta.get("chunk_id")
                        or meta.get("source_id")
                        or ""
                    ),
                    "title": str(title),
                    "url": str(url),
                    "content": result.content,
                    "metadata": {
                        "score": result.score,
                        "author": str(result_author),
                        "doc_type": str(meta.get("doc_type", "")),
                        "timestamp": str(meta.get("timestamp", "")),
                        "chunk_id": str(meta.get("chunk_id", "")),
                        "trust": str(meta.get("trust", "")),
                        "matched_queries": meta["matched_queries"],
                        **{
                            key: meta[key]
                            for key in (
                                "section",
                                "jurisdiction",
                                "version",
                                "source_instance_id",
                                "source_instance_name",
                                "adapter_id",
                                "config_version",
                            )
                            if key in meta
                        },
                    },
                }
            )

        formatted = "\n".join(lines).rstrip()

        return ToolResult(
            tool_name="knowledge_search",
            content=formatted,
            success=True,
            metadata={
                "num_results": len(results),
                "queries": queries,
                "evidence": {
                    "provider": "knowledge_search",
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "records": evidence_records,
                },
            },
        )


def _merge_query_results(
    queries: list[str],
    batches: list[list[RetrievalResult]],
    top_k: int,
) -> list[RetrievalResult]:
    """Fuse ranks rather than incomparable BM25/semantic scores.

    Deduplicate chunks, never entire documents: distinct sections and versions
    must remain available for reasoning and conflict assessment.
    """
    results: dict[tuple, RetrievalResult] = {}
    scores: dict[tuple, float] = {}
    matches: dict[tuple, list[str]] = {}
    for query, batch in zip(queries, batches):
        seen: set[tuple] = set()
        for rank, result in enumerate(batch, start=1):
            meta = result.metadata or {}
            key = (
                (result.source, str(meta["chunk_id"]))
                if meta.get("chunk_id")
                else (
                    result.source,
                    str(meta.get("doc_id", "")),
                    str(meta.get("url", "")),
                    str(meta.get("chunk_index", "")),
                    result.content,
                )
            )
            if key in seen:
                continue
            seen.add(key)
            results.setdefault(key, result)
            scores[key] = scores.get(key, 0.0) + 1.0 / (60 + rank)
            matches.setdefault(key, []).append(query)
    ordered = sorted(results, key=lambda key: -scores[key])
    return [
        replace(
            results[key],
            metadata={**(results[key].metadata or {}), "matched_queries": matches[key]},
        )
        for key in ordered[:top_k]
    ]


__all__ = ["KnowledgeSearchTool"]
