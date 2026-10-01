"""General-source ingestion, persistence, scope, and evidence acceptance."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from openjarvis.connectors.local_files import LocalFilesConnector
from openjarvis.connectors.pipeline import IngestionPipeline
from openjarvis.connectors.store import KnowledgeStore
from openjarvis.tools.knowledge_search import KnowledgeSearchTool


@pytest.fixture()
def connector(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    source = LocalFilesConnector(config_path=str(tmp_path / "config.json"))
    source.configure_path(str(root))
    return source, root


@pytest.mark.parametrize(
    "name,content,expected",
    [
        ("a.txt", "Migration approval", "Migration approval"),
        ("a.md", "# Migration approval", "Migration approval"),
        ("a.csv", "name,status\nMigration,approved", "Migration,approved"),
        ("a.json", '{"Migration":"approved"}', "Migration"),
        (
            "a.html",
            "<p>Migration &amp; approval</p><script>Hidden</script>",
            "Migration & approval",
        ),
    ],
)
def test_formats_are_normalized_with_provenance(connector, name, content, expected):
    source, root = connector
    (root / name).write_text(content)
    documents = list(source.sync())
    assert len(documents) == 1
    assert expected in documents[0].content
    assert "Hidden" not in documents[0].content
    assert documents[0].source == "local_files"
    assert documents[0].metadata["version"]
    assert documents[0].metadata["trust"] == "auto"
    assert documents[0].url == (root / name).as_uri()
    assert source.sync_status().items_synced == 1


def test_persistence_and_failed_reconfiguration_preserve_scope(connector, tmp_path):
    source, root = connector
    reopened = LocalFilesConnector(config_path=str(tmp_path / "config.json"))
    assert reopened.is_connected()
    with pytest.raises(OSError):
        reopened.configure_path(str(tmp_path / "missing"))
    assert json.loads((tmp_path / "config.json").read_text())["path"] == str(root)
    source.disconnect()
    assert not reopened.is_connected()


def test_hidden_binary_and_symlink_files_stay_outside_index(connector, tmp_path):
    source, root = connector
    (root / "allowed.txt").write_text("Allowed")
    (root / ".secret.txt").write_text("Hidden")
    (root / "binary.exe").write_bytes(b"binary")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("Secret")
    (root / "link.txt").symlink_to(outside / "secret.txt")
    (root / "linked-directory").symlink_to(outside, target_is_directory=True)
    assert [doc.title for doc in source.sync()] == ["allowed.txt"]


def test_same_relative_name_in_different_roots_has_distinct_identity(tmp_path):
    identities = []
    for name in ("one", "two"):
        root = tmp_path / name
        root.mkdir()
        (root / "policy.txt").write_text("Policy")
        source = LocalFilesConnector(config_path=str(tmp_path / f"{name}.json"))
        source.configure_path(str(root))
        identities.append(next(source.sync()).doc_id)
    assert identities[0] != identities[1]


def test_sync_applies_since_and_reports_invalid_text(connector):
    source, root = connector
    (root / "old.txt").write_text("Old")
    assert list(source.sync(since=datetime.now(timezone.utc) + timedelta(days=1))) == []
    (root / "old.txt").write_bytes(b"\xff\xfe")
    with pytest.raises(UnicodeDecodeError):
        list(source.sync())
    assert source.sync_status().state == "error"


def test_changed_files_refresh_search_and_emit_evidence(connector):
    source, root = connector
    path = root / "policy.txt"
    path.write_text("Migration approved")
    with KnowledgeStore(":memory:") as store:
        pipeline = IngestionPipeline(store)
        assert pipeline.ingest(source.sync()) == 1
        assert pipeline.ingest(source.sync()) == 0
        path.write_text("Migration canceled")
        assert pipeline.ingest(source.sync()) == 1
        result = KnowledgeSearchTool(store).execute(query="Migration")
        assert "canceled" in result.content and "approved" not in result.content
        record = result.metadata["evidence"]["records"][0]
        assert record["source"] == "local_files"
        assert record["metadata"]["version"]


def test_read_capabilities_are_explicit(connector):
    source, _ = connector
    assert source.capability_requirements() == (
        "connector:local_files:read",
        "file:read",
    )


def test_pdf_text_enters_the_normal_document_pipeline(connector):
    pdfgen = pytest.importorskip("reportlab.pdfgen.canvas")
    pytest.importorskip("pdfplumber")
    source, root = connector
    canvas = pdfgen.Canvas(str(root / "policy.pdf"))
    canvas.drawString(72, 720, "Migration policy approved")
    canvas.save()
    document = next(source.sync())
    assert "Migration policy approved" in document.content
    assert "Page 1" in document.content
    assert document.metadata["format"] == "pdf"


def test_changing_scope_requires_disconnect(connector, tmp_path):
    source, root = connector
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="Disconnect"):
        source.configure_path(str(other))
    with pytest.raises(ValueError):
        source.configure_path(" ")
    assert source._root() == root
    source.disconnect()
    source.configure_path(str(other))
    assert source._root() == other


def test_size_limit_reports_error_without_reading_unbounded_data(
    connector, monkeypatch
):
    from openjarvis.connectors import local_files

    source, root = connector
    (root / "large.txt").write_text("0123456789")
    monkeypatch.setattr(local_files, "_MAX_BYTES", 4)
    with pytest.raises(ValueError, match="ingestion limit"):
        list(source.sync())
    assert source.sync_status().state == "error"


def test_replaced_directory_symlink_cannot_escape_open_scope(connector, tmp_path):
    from openjarvis.connectors.local_files import _open_scoped

    _, root = connector
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("Private")
    (root / "subdir").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        _open_scoped(root, Path("subdir/secret.txt"))


def test_emptying_file_removes_prior_searchable_content(connector):
    source, root = connector
    path = root / "policy.txt"
    path.write_text("Migration approval")
    with KnowledgeStore(":memory:") as store:
        pipeline = IngestionPipeline(store)
        pipeline.ingest(source.sync())
        path.write_text("")
        pipeline.ingest(source.sync())
        assert not store.retrieve("Migration")
