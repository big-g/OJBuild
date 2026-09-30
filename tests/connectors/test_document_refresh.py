"""Knowledge maintenance must replace stale versions without losing good data."""

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from openjarvis.connectors._stubs import Attachment, Document
from openjarvis.connectors.attachment_store import AttachmentStore
from openjarvis.connectors.pipeline import IngestionPipeline
from openjarvis.connectors.store import KnowledgeStore
from openjarvis.core.events import EventBus, EventType


def _doc(**changes):
    defaults = dict(
        doc_id="notes:policy",
        source="notes",
        doc_type="document",
        content="Original bicycle policy has an obsolete clause.",
        title="Bicycle policy",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        metadata={"version": "1", "jurisdiction": "local", "trust": "trusted"},
    )
    defaults.update(changes)
    return Document(**defaults)


def test_refresh_replaces_stale_chunks_and_survives_restart(tmp_path):
    path = tmp_path / "knowledge.db"
    doc = _doc(
        content=" ".join(f"Obsolete bicycle clause number {i}." for i in range(20))
    )
    with KnowledgeStore(path) as store:
        pipeline = IngestionPipeline(store, max_tokens=10)
        assert pipeline.ingest([doc]) > 1
        assert pipeline.ingest([doc]) == 0
    with KnowledgeStore(path) as store:
        pipeline = IngestionPipeline(store, max_tokens=10)
        assert pipeline.ingest([doc]) == 0
        updated = replace(
            doc,
            content="Current bicycle policy is approved.",
            metadata={
                "version": "2",
                "jurisdiction": "local",
                "trust": "trusted",
            },
        )
        assert pipeline.ingest([updated]) == 1
        assert store.count() == 1
        assert store.retrieve("obsolete") == []
        results = store.retrieve("current")
        assert len(results) == 1
        assert results[0].metadata["version"] == "2"
        assert results[0].metadata["jurisdiction"] == "local"
    with KnowledgeStore(path) as store:
        assert IngestionPipeline(store).ingest([updated]) == 0
        assert store.retrieve("current")[0].content == updated.content


def test_metadata_only_refresh_preserves_trust_filtering():
    with KnowledgeStore(":memory:") as store:
        pipeline = IngestionPipeline(store)
        doc = _doc()
        pipeline.ingest([doc])
        assert store.retrieve("bicycle")
        assert pipeline.ingest([replace(doc, metadata={"trust": "untrusted"})]) == 1
        assert store.retrieve("bicycle") == []


def test_attachment_refresh_removes_replaced_and_deleted_text(tmp_path):
    blobs = AttachmentStore(str(tmp_path / "blobs"))
    try:
        with KnowledgeStore(":memory:") as store:
            pipeline = IngestionPipeline(store, attachment_store=blobs)
            doc = _doc(
                attachments=[
                    Attachment(
                        filename="rules.txt",
                        mime_type="text/plain",
                        size_bytes=8,
                        content=b"Retired attachment clause.",
                    )
                ]
            )
            assert pipeline.ingest([doc]) == 2
            updated = replace(
                doc,
                attachments=[
                    replace(
                        doc.attachments[0],
                        content=b"Revised attachment clause.",
                    )
                ],
            )
            assert pipeline.ingest([updated]) == 2
            assert store.retrieve("retired") == []
            assert store.retrieve("revised")
            assert pipeline.ingest([replace(updated, attachments=[])]) == 1
            assert store.retrieve("revised") == []
            assert store.count() == 1
    finally:
        blobs.close()


def test_preparation_failure_preserves_previous_version(monkeypatch):
    with KnowledgeStore(":memory:") as store:
        pipeline = IngestionPipeline(store)
        doc = _doc()
        pipeline.ingest([doc])
        updated = replace(doc, content="Updated bicycle policy.")

        def fail(content):
            raise RuntimeError("embedding preparation failed")

        monkeypatch.setattr(pipeline, "_embed_chunk", fail)
        with pytest.raises(RuntimeError, match="embedding preparation"):
            pipeline.ingest([updated])
        assert store.count() == 1
        assert store.retrieve("obsolete")[0].content == doc.content
        assert store.document_fingerprint(doc.doc_id) is not None


def test_pdf_extraction_failure_preserves_previous_attachment(tmp_path):
    blobs = AttachmentStore(str(tmp_path / "blobs"))
    try:
        with KnowledgeStore(":memory:") as store:
            pipeline = IngestionPipeline(store, attachment_store=blobs)
            doc = _doc(
                attachments=[
                    Attachment(
                        filename="rules.txt",
                        mime_type="text/plain",
                        size_bytes=7,
                        content=b"Original attachment requirement.",
                    )
                ]
            )
            pipeline.ingest([doc])
            broken = replace(
                doc,
                attachments=[
                    Attachment(
                        filename="rules.pdf",
                        mime_type="application/pdf",
                        size_bytes=6,
                        content=b"not a PDF",
                    )
                ],
            )
            with pytest.raises(RuntimeError, match="Could not extract PDF"):
                pipeline.ingest([broken])
            assert store.count() == 2
            assert store.retrieve("requirement")
            assert pipeline.ingest([doc]) == 0
    finally:
        blobs.close()


def test_commit_failure_rolls_back_chunks_fts_and_events(monkeypatch):
    bus = EventBus(record_history=True)
    monkeypatch.setattr("openjarvis.connectors.store.get_event_bus", lambda: bus)
    with KnowledgeStore(":memory:") as store:
        pipeline = IngestionPipeline(store)
        doc = _doc(source_id="first")
        other = _doc(
            doc_id="notes:other", source_id="second", content="Other document."
        )
        pipeline.ingest([doc, other])
        prior_events = len(bus.history)
        with pytest.raises(sqlite3.IntegrityError):
            pipeline.ingest(
                [replace(doc, source_id="second", content="Updated policy.")]
            )
        assert store.count() == 2
        assert len(bus.history) == prior_events
        assert store.retrieve("obsolete")[0].content == doc.content
        assert store.retrieve("updated") == []
        assert all(
            event.event_type == EventType.MEMORY_STORE for event in bus.history[:2]
        )


def test_existing_pipeline_can_reingest_a_deleted_document():
    with KnowledgeStore(":memory:") as store:
        pipeline = IngestionPipeline(store)
        doc = _doc()
        assert pipeline.ingest([doc]) == 1
        assert store.delete(doc.doc_id)
        assert pipeline.ingest([doc]) == 1
        assert store.retrieve("bicycle")


def test_legacy_chunks_are_refreshed_once():
    with KnowledgeStore(":memory:") as store:
        doc = _doc()
        store.store("Legacy outdated clause.", doc_id=doc.doc_id, source=doc.source)
        pipeline = IngestionPipeline(store)
        assert pipeline.ingest([doc]) == 1
        assert store.retrieve("legacy") == []
        assert pipeline.ingest([doc]) == 0
