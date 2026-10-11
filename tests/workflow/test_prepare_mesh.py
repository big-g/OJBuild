import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import trimesh

spec = importlib.util.spec_from_file_location(
    "prepare_mesh", Path(__file__).parents[2] / "scripts/prepare_workflow_mesh.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_simplifies_transformed_scene_without_changing_original(tmp_path):
    mesh = trimesh.creation.icosphere(subdivisions=4)
    scene = trimesh.Scene()
    transform = trimesh.transformations.translation_matrix([10, 20, 30])
    scene.add_geometry(mesh, transform=transform)
    source, output = tmp_path / "source.glb", tmp_path / "prepared.glb"
    original = scene.export(file_type="glb")
    source.write_bytes(original)
    result = module.prepare(source, output, 1000)
    assert source.read_bytes() == original
    assert result["before"]["faces"] == 5120
    assert 0 < result["after"]["faces"] <= 1000
    assert result["bytes"] <= module.OUTPUT_BYTES
    assert result["after"]["bounds"][0][0] > 8
    assert trimesh.load(output).to_mesh().is_watertight
    with pytest.raises(ValueError, match="never overwritten"):
        module.prepare(source, output, 1000)
    with pytest.raises(ValueError, match="never overwritten"):
        module.prepare(source, source, 1000)


def test_rejects_input_budget_and_invalid_glb(tmp_path, monkeypatch):
    source, output = tmp_path / "source.glb", tmp_path / "prepared.glb"
    source.write_bytes(trimesh.creation.icosphere().export(file_type="glb"))
    monkeypatch.setattr(module, "INPUT_FACES", 10)
    with pytest.raises(ValueError, match="budget|exceeds"):
        module.prepare(source, output)
    assert not output.exists()
    source.write_bytes(b"invalid")
    with pytest.raises(ValueError, match="binary GLB"):
        module.prepare(source, output)


def test_bounded_linux_command(tmp_path):
    source, output = tmp_path / "source.glb", tmp_path / "prepared.glb"
    source.write_bytes(
        trimesh.creation.icosphere(subdivisions=4).export(file_type="glb")
    )
    result = subprocess.run(
        [
            sys.executable,
            str(Path(module.__file__)),
            str(source),
            str(output),
            "--faces",
            "1000",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["after"]["faces"] <= 1000
