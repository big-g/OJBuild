"""IngestionPipeline — deduplicate, chunk, and store Documents.

Takes ``Document`` objects from connectors, deduplicates unchanged versions,
splits content using ``SemanticChunker``, and atomically refreshes changed
documents in a ``KnowledgeStore``.

Typical usage::

    store = KnowledgeStore(db_path=":memory:")
    pipeline = IngestionPipeline(store)
    n_chunks = pipeline.ingest(connector.sync())
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from datetime import datetime
from typing import TYPE_CHECKING, Iterable, Optional

from openjarvis.connectors._stubs import Attachment, Document
from openjarvis.connectors.chunker import SemanticChunker
from openjarvis.connectors.embeddings import OllamaEmbedder
from openjarvis.connectors.store import KnowledgeStore


def _namespace_thread_id(source: str, thread_id: Optional[str]) -> Optional[str]:
    """Prefix ``thread_id`` with ``{source}:`` so it can't collide across sources.

    Idempotent: if the input already starts with ``{source}:`` it is returned
    unchanged. Centralised here (rather than per-connector) so a new connector
    author can't forget to namespace.
    """
    if not thread_id:
        return None
    prefix = f"{source}:"
    if thread_id.startswith(prefix):
        return thread_id
    return f"{prefix}{thread_id}"


def _derive_source_id(doc: Document) -> str:
    """Return the connector-set ``source_id`` or extract it from ``doc_id``.

    Many existing connectors compose ``doc_id = f"{source}:{native_id}"``;
    this strips the prefix so storage can index the native ID directly.
    """
    if doc.source_id:
        return doc.source_id
    prefix = f"{doc.source}:"
    if doc.doc_id.startswith(prefix):
        return doc.doc_id[len(prefix) :]
    return doc.doc_id


def _content_hash(text: str) -> str:
    """SHA-256 hex digest of UTF-8-encoded chunk content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _document_hash(doc: Document) -> str:
    """Fingerprint body, provenance, custom metadata, and attachment bytes."""

    def encode(value):
        if isinstance(value, bytes):
            return {"sha256": hashlib.sha256(value).hexdigest()}
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError(f"Unsupported document metadata type: {type(value).__name__}")

    snapshot = json.dumps(asdict(doc), sort_keys=True, default=encode)
    return _content_hash(snapshot)


if TYPE_CHECKING:
    from openjarvis.connectors.attachment_store import AttachmentStore


class IngestionPipeline:
    """Deduplicate, chunk, and index documents into a KnowledgeStore.

    Parameters
    ----------
    store:
        The ``KnowledgeStore`` instance to write chunks into.
    max_tokens:
        Soft upper-limit on chunk size passed to ``SemanticChunker``.
    attachment_store:
        Optional ``AttachmentStore`` for persisting attachment blobs and
        extracting text from supported MIME types (PDF, plain text, etc.).
        When ``None`` (default) attachments are silently ignored.
    embedder:
        Optional embedding client (e.g. ``OllamaEmbedder``). When provided,
        every chunk is embedded at ingest time and the resulting float32
        vector is written to the ``embedding`` BLOB column alongside
        ``embedding_model_version``. ``None`` (default) skips embedding so
        in-memory tests and offline runs don't depend on a sidecar daemon.
    """

    def __init__(
        self,
        store: KnowledgeStore,
        *,
        max_tokens: int = 512,
        attachment_store: Optional[AttachmentStore] = None,
        embedder: Optional[OllamaEmbedder] = None,
    ) -> None:
        self._store = store
        self._chunker = SemanticChunker(max_tokens=max_tokens)
        self._attachment_store = attachment_store
        self._embedder = embedder

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _embed_chunk(self, content: str) -> tuple[Optional[bytes], str]:
        """Return ``(embedding_bytes, model_version)`` for a chunk.

        Returns ``(None, "")`` when no embedder is configured or the embedder
        fails — ingestion continues with the lexical-only row, so a flaky
        local daemon never blocks a sync.
        """
        if self._embedder is None:
            return None, ""
        emb = self._embedder.embed(content)
        if emb is None:
            return None, ""
        return emb, self._embedder.model_version

    def _extract_attachment_text(self, att: Attachment) -> str:
        """Extract text from an attachment.

        Returns empty text for unsupported formats. A PDF parsing failure
        raises so a refresh cannot silently discard previously indexed text.
        """
        if att.mime_type == "application/pdf":
            try:
                import io

                import pdfplumber

                with pdfplumber.open(io.BytesIO(att.content)) as pdf:
                    return "\n".join(page.extract_text() or "" for page in pdf.pages)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"Could not extract PDF attachment: {att.filename}"
                ) from exc
        if att.mime_type in ("text/plain", "text/markdown", "text/csv"):
            return att.content.decode("utf-8", errors="replace")
        return ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ingest(self, documents: Iterable[Document]) -> int:
        """Deduplicate unchanged documents and atomically refresh changed versions.

        Each version is fully chunked and embedded before replacing the indexed
        version. Failure leaves the previous searchable chunks intact. Existing
        rows without version fingerprints are refreshed once on their next sync.
        Returns the number of chunks written, including refreshed chunks.
        Repeated document IDs within one batch retain the first occurrence.
        """
        chunks_stored = 0
        seen_in_batch = set()
        for doc in documents:
            if doc.doc_id in seen_in_batch:
                continue
            seen_in_batch.add(doc.doc_id)
            fingerprint = _document_hash(doc)
            if self._store.document_fingerprint(doc.doc_id) == fingerprint:
                continue
            with KnowledgeStore(":memory:", publish_events=False) as staged:
                self._write_document(doc, staged, fingerprint)
                chunks_stored += self._store.replace_document(doc.doc_id, staged)
        return chunks_stored

    def _write_document(
        self, doc: Document, store: KnowledgeStore, fingerprint: str
    ) -> int:
        """Prepare a complete document version in an isolated staging store."""
        chunks_stored = 0
        # Compute v1 provenance fields once per document.
        namespaced_thread = _namespace_thread_id(doc.source, doc.thread_id)
        source_id = _derive_source_id(doc)
        ingest_epoch = time.time()

        # Build the parent metadata dict that will be inherited by every
        # chunk produced from this document.
        parent_meta = {
            "title": doc.title,
            "author": doc.author,
            "source": doc.source,
            "source_id": source_id,
            "doc_type": doc.doc_type,
            "url": doc.url or "",
            "thread_id": namespaced_thread or "",
            "channel": doc.channel or "",
        }
        # Merge any extra connector-level metadata (without overwriting
        # the standard provenance fields set above).
        parent_meta.update(doc.metadata)
        parent_meta["openjarvis_document_hash"] = fingerprint

        # Normalise the timestamp to a string once.
        if hasattr(doc.timestamp, "isoformat"):
            timestamp_str = doc.timestamp.isoformat()
        else:
            timestamp_str = str(doc.timestamp)

        # Chunk the document content using the type-aware strategy.
        chunks = self._chunker.chunk(
            doc.content,
            doc_type=doc.doc_type,
            metadata=parent_meta,
        )

        for chunk in chunks:
            embedding_bytes, embedding_version = self._embed_chunk(chunk.content)
            store.store(
                content=chunk.content,
                source=doc.source,
                source_id=source_id,
                doc_type=doc.doc_type,
                doc_id=doc.doc_id,
                title=doc.title,
                author=doc.author,
                participants=doc.participants,
                participants_raw=doc.participants_raw,
                timestamp=timestamp_str,
                thread_id=namespaced_thread,
                channel=doc.channel,
                url=doc.url,
                metadata=chunk.metadata,
                chunk_index=chunk.index,
                content_hash=_content_hash(chunk.content),
                embedding=embedding_bytes,
                embedding_model_version=embedding_version,
                last_synced=ingest_epoch,
            )
            chunks_stored += 1

        # Process attachments when an attachment store is configured.
        if self._attachment_store and doc.attachments:
            for att in doc.attachments:
                if not att.content:
                    continue

                # Persist the raw blob and obtain its SHA-256.
                sha = self._attachment_store.store(
                    content=att.content,
                    filename=att.filename,
                    mime_type=att.mime_type,
                    source_doc_id=doc.doc_id,
                )

                # Extract searchable text and index it as additional chunks.
                extracted = self._extract_attachment_text(att)
                if extracted:
                    att_chunks = self._chunker.chunk(
                        extracted,
                        doc_type=doc.doc_type,
                        metadata={
                            **parent_meta,
                            "attachment": att.filename,
                            "sha256": sha,
                        },
                    )
                    # Synthetic source_id keeps attachment chunks distinct
                    # from body chunks under the UNIQUE(source, source_id,
                    # chunk_index) constraint while still letting them share
                    # a parent doc_id for dedup and blob linkage.
                    att_source_id = f"{source_id}#{att.filename}"
                    for chunk in att_chunks:
                        embedding_bytes, embedding_version = self._embed_chunk(
                            chunk.content
                        )
                        store.store(
                            content=chunk.content,
                            source=doc.source,
                            source_id=att_source_id,
                            doc_type=doc.doc_type,
                            doc_id=doc.doc_id,
                            title=f"{doc.title} [{att.filename}]",
                            author=doc.author,
                            participants=doc.participants,
                            participants_raw=doc.participants_raw,
                            timestamp=timestamp_str,
                            thread_id=namespaced_thread,
                            channel=doc.channel,
                            url=doc.url,
                            metadata=chunk.metadata,
                            chunk_index=chunk.index,
                            content_hash=_content_hash(chunk.content),
                            embedding=embedding_bytes,
                            embedding_model_version=embedding_version,
                            last_synced=ingest_epoch,
                        )
                        chunks_stored += 1

        return chunks_stored


__all__ = ["IngestionPipeline"]
