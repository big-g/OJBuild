"""Tests for the retrieval tool."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from openjarvis.tools.retrieval import RetrievalTool
from openjarvis.tools.storage._stubs import MemoryBackend, RetrievalResult


class _FakeBackend(MemoryBackend):
    """In-memory fake backend for testing."""

    backend_id = "fake"

    def __init__(self, results: Optional[List[RetrievalResult]] = None) -> None:
        self._results = results or []

    def store(
        self,
        content: str,
        *,
        source: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        return "fake-id"

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        **kwargs: Any,
    ) -> List[RetrievalResult]:
        return self._results[:top_k]

    def delete(self, doc_id: str) -> bool:
        return False

    def clear(self) -> None:
        self._results.clear()


class _ErrorBackend(_FakeBackend):
    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        **kwargs: Any,
    ) -> List[RetrievalResult]:
        raise RuntimeError("backend error")


class TestRetrievalTool:
    def test_spec(self):
        tool = RetrievalTool()
        assert tool.spec.name == "retrieval"
        assert tool.spec.category == "memory"
        assert tool.spec.evidence_kinds == ["external"]

    def test_no_backend(self):
        tool = RetrievalTool()
        result = tool.execute(query="test")
        assert result.success is False
        assert "No memory backend" in result.content

    def test_empty_query(self):
        tool = RetrievalTool(backend=_FakeBackend())
        result = tool.execute(query="")
        assert result.success is False

    def test_no_results(self):
        tool = RetrievalTool(backend=_FakeBackend())
        result = tool.execute(query="test")
        assert result.success is True
        assert "No relevant results" in result.content

    def test_with_results(self):
        results = [
            RetrievalResult(content="Answer 1", score=0.9, source="doc.md"),
            RetrievalResult(content="Answer 2", score=0.8, source="other.md"),
        ]
        tool = RetrievalTool(backend=_FakeBackend(results))
        result = tool.execute(query="test")
        assert result.success is True
        assert "Answer 1" in result.content
        assert "[Source: doc.md]" in result.content
        assert result.metadata["num_results"] == 2

    def test_results_emit_structured_evidence_provenance(self):
        results = [
            RetrievalResult(
                content="Answer 1",
                score=0.9,
                source="doc.md",
                metadata={
                    "doc_id": "doc-1",
                    "chunk_id": "chunk-1",
                    "title": "Document One",
                    "url": "file:///doc.md",
                    "timestamp": "2026-09-01T12:00:00Z",
                    "trust": "trusted",
                },
            )
        ]
        tool = RetrievalTool(backend=_FakeBackend(results))

        result = tool.execute(query="test")

        assert result.success is True
        assert result.metadata["evidence"]["provider"] == "memory_retrieval"
        records = result.metadata["evidence"]["records"]
        assert len(records) == 1
        assert records[0]["source"] == "doc.md"
        assert records[0]["source_id"] == "doc-1"
        assert records[0]["title"] == "Document One"
        assert records[0]["content"] == "Answer 1"
        assert records[0]["metadata"]["chunk_id"] == "chunk-1"

    def test_untrusted_results_are_excluded_from_output_and_evidence(self):
        results = [
            RetrievalResult(
                content="Trusted answer",
                score=0.9,
                source="trusted.md",
                metadata={"trust": "trusted", "doc_id": "trusted-1"},
            ),
            RetrievalResult(
                content="Quarantined answer",
                score=0.95,
                source="bad.md",
                metadata={"trust": "untrusted", "doc_id": "bad-1"},
            ),
            RetrievalResult(
                content="Unknown-tier answer",
                score=0.99,
                source="future.md",
                metadata={"trust": "future-tier", "doc_id": "future-1"},
            ),
        ]
        tool = RetrievalTool(backend=_FakeBackend(results))

        result = tool.execute(query="test")

        assert result.success is True
        assert "Trusted answer" in result.content
        assert "Quarantined answer" not in result.content
        assert "Unknown-tier answer" not in result.content
        records = result.metadata["evidence"]["records"]
        assert [record["source_id"] for record in records] == ["trusted-1"]

    def test_top_k_override(self):
        results = [
            RetrievalResult(content="A", score=0.9),
            RetrievalResult(content="B", score=0.8),
            RetrievalResult(content="C", score=0.7),
        ]
        tool = RetrievalTool(backend=_FakeBackend(results), top_k=10)
        result = tool.execute(query="test", top_k=1)
        assert result.success is True
        assert "A" in result.content

    def test_backend_error(self):
        tool = RetrievalTool(backend=_ErrorBackend())
        result = tool.execute(query="test")
        assert result.success is False
        assert "Retrieval error" in result.content

    def test_openai_function(self):
        tool = RetrievalTool()
        fn = tool.to_openai_function()
        assert fn["function"]["name"] == "retrieval"
