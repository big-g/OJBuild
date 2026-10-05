import base64
import os
import struct
from concurrent.futures import ThreadPoolExecutor

import pytest

from openjarvis.artifacts.preview import MAX_FACETS, preview
from openjarvis.artifacts.store import ArtifactError, ArtifactNotFound, ArtifactStore
from openjarvis.core.correlation import ExecutionIdentity, execution_scope
from openjarvis.security.file_policy import is_sensitive_file
from openjarvis.tools.artifact_save import ArtifactSaveTool
from openjarvis.tools.file_read import FileReadTool
from openjarvis.tools.file_write import FileWriteTool


def test_owner_isolation_and_integrity(tmp_path):
    store = ArtifactStore(tmp_path / "files")
    item = store.save("a", "test.py", 'print("hello")')
    assert store.list("b") == []
    assert store.read("a", item["id"])[1] == b'print("hello")'
    with pytest.raises(ArtifactNotFound):
        store.read("b", item["id"])
    with pytest.raises(ArtifactNotFound):
        store.delete("b", item["id"])
    blob = next(store.root.glob("*/*.blob"))
    assert blob.stat().st_mode & 0o777 == 0o600
    assert blob.parent.stat().st_mode & 0o777 == 0o700
    assert is_sensitive_file(blob)
    assert not FileReadTool().execute(path=str(blob)).success
    assert not FileWriteTool().execute(path=str(blob), content="changed").success
    blob.write_bytes(b"tampered")
    with pytest.raises(ArtifactError, match="integrity"):
        store.read("a", item["id"])
    store.delete("a", item["id"])
    assert store.list("a") == []
    assert not blob.exists()


@pytest.mark.parametrize(
    "filename", ["../x", "a/b", "a\\b", "", ".", "..", "x\n.py", "\x00"]
)
def test_unsafe_names_rejected(tmp_path, filename):
    with pytest.raises(ArtifactError):
        ArtifactStore(tmp_path / "files").save("a", filename, "content")


def test_invalid_identity_and_encoding(tmp_path):
    store = ArtifactStore(tmp_path / "files")
    for args in [
        ("", "x", "a"),
        ("a", "x", "***", "base64"),
        ("a", "x", 42),
        ("a", "x", "a", "unknown"),
    ]:
        with pytest.raises(ArtifactError):
            store.save(*args)
    item = store.save(
        "a", "binary.stl", base64.b64encode(b"\x00\xff").decode(), "base64"
    )
    assert store.read("a", item["id"])[1] == b"\x00\xff"
    with pytest.raises(ArtifactNotFound):
        store.read("a", "../x")


def test_symlink_and_hardlink_fail_closed(tmp_path):
    store = ArtifactStore(tmp_path / "files")
    item = store.save("a", "a.py", "content")
    blob = next(store.root.glob("*/*.blob"))
    target = tmp_path / "secret"
    target.write_text("private")
    blob.unlink()
    blob.symlink_to(target)
    with pytest.raises(ArtifactError):
        store.read("a", item["id"])
    blob.unlink()
    os.link(target, blob)
    with pytest.raises(ArtifactError):
        store.read("a", item["id"])
    alias = tmp_path / "alias"
    alias.symlink_to(store.root, target_is_directory=True)
    with pytest.raises(OSError):
        ArtifactStore(alias / "nested")


def test_parallel_quota_is_atomic(tmp_path, monkeypatch):
    from openjarvis.artifacts import store as module

    monkeypatch.setattr(module, "MAX_USER_FILES", 1)
    store = ArtifactStore(tmp_path / "files")

    def save(index):
        try:
            return store.save("a", f"{index}.txt", "abc")
        except ArtifactError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(save, range(4)))
    assert sum(result is not None for result in results) == 1
    assert len(list(store.root.glob("*/*.blob"))) == 1
    monkeypatch.setattr(module, "MAX_USER_BYTES", 2)
    with pytest.raises(ArtifactError, match="quota"):
        store.save("b", "file", "abc")
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 1)
    with pytest.raises(ArtifactError, match="20 MiB"):
        store.save("b", "file", "abc")


def test_tool_uses_context_owner_not_model_parameter(tmp_path):
    store = ArtifactStore(tmp_path / "files")
    tool = ArtifactSaveTool(store)
    assert not tool.execute(filename="x", content="x").success
    with execution_scope(ExecutionIdentity(user_id="user-a")):
        assert not tool.execute(filename="x", content="x", owner="user-b").success
        assert tool.execute(filename="x.py", content="print(1)").success
    assert len(store.list("user-a")) == 1
    assert store.list("user-b") == []
    assert tool.spec.required_capabilities == ["file:write"]


def test_previews_never_execute_and_are_bounded():
    active = b'<script>throw new Error("must not execute")</script>'
    result = preview("x.html", active)
    assert result["kind"] == "text" and result["text"] == active.decode()
    result = preview("x.py", b"a" * 100000)
    assert result["truncated"] and len(result["text"]) == 65536
    assert preview("x.pdf", b"\x00\xff")["kind"] == "bytes"
    ascii_stl = (b"solid t\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\n"
                 b"vertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid")
    assert preview("model.stl", ascii_stl)["triangles"] == [[0, 0, 0, 1, 0, 0, 0, 1, 0]]
    facet = struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0)
    binary = b"x" * 80 + struct.pack("<I", MAX_FACETS + 1) + facet * (MAX_FACETS + 1)
    result = preview("binary.stl", binary)
    assert result["kind"] == "stl" and result["truncated"]
    assert len(result["triangles"]) == MAX_FACETS
    bad = b"x" * 80 + struct.pack("<I", 0xFFFFFFFF)
    assert preview("bad.stl", bad)["kind"] != "stl"
    assert (
        preview("bad.stl", ascii_stl.replace(b"vertex 1", b"vertex 1e999"))["kind"]
        != "stl"
    )
