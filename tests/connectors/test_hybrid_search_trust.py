"""Keep quarantined knowledge out of Deep Research context and evidence."""

from __future__ import annotations

import json
import struct

import pytest

from openjarvis.agents.research_loop import shape_results_for_model
from openjarvis.connectors.hybrid_search import HybridSearch
from openjarvis.connectors.store import KnowledgeStore


@pytest.fixture()
def store():
    with KnowledgeStore(":memory:") as knowledge:
        yield knowledge


def _add(store, name, *, metadata=None, **kwargs):
    return store.store(
        content=f"Migration {name}",
        title=name,
        source="gcalendar",
        timestamp="2999-01-01T09:00:00+00:00",
        metadata=metadata,
        **kwargs,
    )


@pytest.mark.parametrize("query", ["Migration", "", "unmatchedxyz"])
@pytest.mark.parametrize("trust", ["untrusted", "unknown", 7, ["trusted"]])
def test_ranked_and_fallback_search_exclude_quarantined_rows(store, query, trust):
    _add(store, "Quarantined", metadata={"trust": trust})
    allowed = _add(store, "Allowed", metadata={"trust": "trusted"})

    hits = HybridSearch(store, recall_k=1).search(query, limit=1)

    assert [hit.chunk_id for hit in hits] == [allowed]
    assert "Quarantined" not in json.dumps(shape_results_for_model(hits))


@pytest.mark.parametrize("raw", ["broken{", "[]", '"trusted"', "null"])
def test_corrupt_or_nonobject_metadata_fails_closed(store, raw):
    quarantined = _add(store, "Quarantined")
    allowed = _add(store, "Allowed")
    store._conn.execute(
        "UPDATE knowledge_chunks SET metadata = ? WHERE id = ?",
        (raw, quarantined),
    )
    store._conn.commit()

    for query in ("Migration", "", "unmatchedxyz"):
        hits = HybridSearch(store).search(query)
        assert [hit.chunk_id for hit in hits] == [allowed]


@pytest.mark.parametrize("trust", [None, "", "auto", "trusted"])
def test_legacy_and_recallable_tiers_remain_searchable(store, trust):
    metadata = {} if trust is None else {"trust": trust}
    allowed = _add(store, "Allowed", metadata=metadata)
    assert [h.chunk_id for h in HybridSearch(store).search("Migration")] == [allowed]


def test_dense_recall_filters_before_limit(store):
    # The quarantined vector is a perfect match; the allowed vector is weaker.
    # Filtering after top-k would lose the allowed candidate entirely.
    _add(
        store, "Quarantined", metadata={"trust": "untrusted"},
        embedding=struct.pack("ff", 1.0, 0.0),
    )
    allowed = _add(store, "Allowed", embedding=struct.pack("ff", 0.8, 0.2))

    class Embedder:
        def embed(self, query):
            return struct.pack("ff", 1.0, 0.0)

    hits = HybridSearch(store, Embedder(), recall_k=1).search("semanticxyz", limit=1)
    assert [hit.chunk_id for hit in hits] == [allowed]
    assert hits[0].vector_score > 0


@pytest.mark.parametrize("query", ["next calendar events", "upcoming Migration events"])
def test_calendar_timeline_excludes_quarantined_nearest_event(store, query):
    _add(store, "Quarantined", metadata={"trust": "untrusted"})
    allowed = _add(store, "Allowed")
    store._conn.execute(
        "UPDATE knowledge_chunks SET timestamp = ? WHERE id = ?",
        ("2999-01-02T09:00:00+00:00", allowed),
    )
    store._conn.commit()

    hits = HybridSearch(store).search(query, limit=1)
    assert [hit.chunk_id for hit in hits] == [allowed]


@pytest.mark.parametrize("query", ["Migration", "", "next calendar events"])
def test_quarantined_only_corpus_returns_no_hits(store, query):
    _add(store, "Quarantined", metadata={"trust": "untrusted"})
    assert HybridSearch(store).search(query) == []


def test_thread_context_cannot_reintroduce_quarantined_evidence(store):
    _add(store, "Quarantined", metadata={"trust": "untrusted"}, thread_id="thread")
    _add(store, "Allowed", thread_id="thread", chunk_index=1)
    deleted = _add(store, "Deleted", thread_id="thread", chunk_index=2)
    store._conn.execute(
        "UPDATE knowledge_chunks SET deleted_at = 1 WHERE id = ?", (deleted,)
    )
    store._conn.commit()

    hits = HybridSearch(store, thread_context_cap=1).search("Allowed", limit=1)
    payload = json.dumps(shape_results_for_model(hits))
    assert len(hits[0].thread_context) == 1
    assert hits[0].thread_context[0]["snippet"] == "Migration Allowed"
    assert "Quarantined" not in payload
    assert "Deleted" not in payload


def test_materialisation_rechecks_trust_after_ranking(store):
    candidate = _add(store, "Quarantined")

    class QuarantineDuringSearch(HybridSearch):
        def _fuse(self, bm25, vector):
            fused = super()._fuse(bm25, vector)
            store._conn.execute(
                "UPDATE knowledge_chunks SET metadata = ? WHERE id = ?",
                ('{"trust":"untrusted"}', candidate),
            )
            store._conn.commit()
            return fused

    assert QuarantineDuringSearch(store).search("Migration") == []
