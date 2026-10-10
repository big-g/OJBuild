"""Fixed mesh operations in a disposable, resource-bounded child process."""

from __future__ import annotations

import io
import json
import struct
import sys
from pathlib import Path

MAX_FACES = 400_000
MAX_VERTICES = 800_000


def check_glb(data):
    if len(data) < 20 or data[:4] != b"glTF":
        raise ValueError("Expected a binary GLB file")
    _, version, length = struct.unpack_from("<III", data)
    if version != 2 or length != len(data):
        raise ValueError("Invalid GLB header")
    size, kind = struct.unpack_from("<II", data, 12)
    if kind != 0x4E4F534A or size > len(data) - 20:
        raise ValueError("Invalid GLB document")
    document = json.loads(data[20 : 20 + size])
    for entry in document.get("buffers", []) + document.get("images", []):
        if "uri" in entry:
            raise ValueError(
                "GLB must contain embedded resources, without external URIs"
            )
    for entry in document.get("accessors", []):
        count = entry.get("count", 0)
        if not isinstance(count, int) or count < 0 or count > MAX_VERTICES * 3:
            raise ValueError("GLB accessor exceeds mesh limits")


def load(data, kind):
    import numpy as np
    import trimesh

    if kind not in {"glb", "stl", "ply"}:
        raise ValueError("Use a GLB, STL or workflow PLY artifact")
    if kind == "glb":
        check_glb(data)
    scene = trimesh.load(
        io.BytesIO(data),
        file_type=kind,
        force="scene",
        skip_materials=True,
    )
    if len(scene.geometry) > 128 or len(scene.graph.nodes_geometry) > 128:
        raise ValueError("Mesh scene exceeds instance limit")
    estimate = sum(
        len(g.faces) for g in scene.geometry.values() if isinstance(g, trimesh.Trimesh)
    )
    if estimate * max(1, len(scene.graph.nodes_geometry)) > MAX_FACES:
        raise ValueError("Mesh exceeds face limit")
    # Applies each instance's world transform; do not concatenate raw geometries.
    mesh = scene.to_mesh()
    mesh.metadata["workflow_source_meshes"] = len(scene.geometry)
    mesh.metadata["workflow_source_instances"] = len(scene.graph.nodes_geometry)
    if (
        not len(mesh.faces)
        or len(mesh.faces) > MAX_FACES
        or len(mesh.vertices) > MAX_VERTICES
        or not np.isfinite(mesh.vertices).all()
    ):
        raise ValueError("Mesh is empty, non-finite or exceeds processing limits")
    return mesh


def report(mesh):
    import numpy as np
    import trimesh

    components = trimesh.graph.connected_components(
        mesh.face_adjacency,
        nodes=np.arange(len(mesh.faces)),
        min_len=1,
    )
    return {
        "vertices": len(mesh.vertices),
        "source_meshes": mesh.metadata.get("workflow_source_meshes", 1),
        "source_instances": mesh.metadata.get("workflow_source_instances", 1),
        "faces": len(mesh.faces),
        "degenerate_faces": int((~mesh.nondegenerate_faces()).sum()),
        "duplicate_faces": int((~mesh.unique_faces()).sum()),
        "components": len(components),
        "bounds": mesh.bounds.tolist(),
        "dimensions": mesh.extents.tolist(),
        "watertight": bool(mesh.is_watertight),
        "winding_consistent": bool(mesh.is_winding_consistent),
        "normals_finite": bool(np.isfinite(mesh.face_normals).all()),
        "volume": float(mesh.volume) if mesh.is_volume else None,
        "printability_verified": False,
        "warning": "Watertightness does not certify printability. Check in a slicer.",
    }


def process(input_path, output_path, operation, params):
    import numpy as np
    import trimesh

    mesh = load(input_path.read_bytes(), input_path.suffix[1:])
    before = report(mesh)
    if operation == "mesh_repair":
        mesh.update_faces(mesh.unique_faces())
        mesh.update_faces(mesh.nondegenerate_faces())
        mesh.remove_unreferenced_vertices()
        mesh.merge_vertices()
        trimesh.repair.fix_normals(mesh, multibody=True)
        if params.get("fill_small_holes", True):
            trimesh.repair.fill_holes(mesh)
        if params.get("keep_largest", False):
            parts = mesh.split(only_watertight=False)
            if not len(parts):
                raise ValueError("Repair removed all mesh components")
            mesh = max(parts, key=lambda part: len(part.faces))
    elif operation == "mesh_scale":
        axis = params.get("axis", "longest")
        extent = float(
            max(mesh.extents)
            if axis == "longest"
            else mesh.extents[{"x": 0, "y": 1, "z": 2}[axis]]
        )
        if extent <= 0:
            raise ValueError("Chosen mesh dimension is zero")
        mesh.apply_scale(params["target_mm"] / extent)
        mesh.apply_translation(-mesh.bounds[0])
    elif operation == "mesh_blender_remesh":
        import shutil
        import subprocess

        blender = shutil.which("blender")
        if not blender:
            raise ValueError("Install Blender on the server to use voxel remeshing")
        voxel = params["voxel_size"]
        if float(np.prod(np.ceil(mesh.extents / voxel) + 1)) > 8_000_000:
            raise ValueError("Voxel size is too small for this mesh; increase it")
        source = output_path.with_suffix(".vertices.json")
        result = output_path.with_suffix(".remeshed.json")
        source.write_text(
            json.dumps(
                {
                    "vertices": mesh.vertices.tolist(),
                    "faces": mesh.faces.tolist(),
                    "voxel_size": voxel,
                }
            )
        )
        subprocess.run(
            [
                blender,
                "--background",
                "--factory-startup",
                "--disable-autoexec",
                "--python",
                str(Path(__file__).with_name("blender_worker.py")),
                "--",
                str(source),
                str(result),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=180,
        )
        if result.stat().st_size > 80 * 1024**2:
            raise ValueError("Remeshed output exceeds processing limits")
        geometry = json.loads(result.read_text())
        mesh = trimesh.Trimesh(**geometry, process=True)
    elif operation not in {"mesh_inspect", "mesh_export_stl"}:
        raise ValueError("Unknown mesh operation")
    if (
        not len(mesh.faces)
        or len(mesh.faces) > MAX_FACES
        or len(mesh.vertices) > MAX_VERTICES
        or not np.isfinite(mesh.vertices).all()
    ):
        raise ValueError("Processed mesh is empty or exceeds limits")
    after = report(mesh)
    if operation == "mesh_export_stl":
        if params.get("require_watertight", True) and not mesh.is_watertight:
            raise ValueError(
                "Mesh is not watertight; repair or change export requirement"
            )
        data = mesh.export(file_type="stl")
        reloaded = load(data, "stl")
        if not np.allclose(mesh.extents, reloaded.extents, rtol=1e-5, atol=1e-5):
            raise ValueError("STL dimensions changed during export")
        after = report(reloaded)
        after["units"] = "mm"
    else:
        data = mesh.export(file_type="ply")
    if operation != "mesh_inspect":
        if len(data) > 20 * 1024**2:
            raise ValueError("Processed file exceeds 20 MiB")
        output_path.write_bytes(data)
    return {
        "before": before,
        "after": after,
        "operation": operation,
        "parameters": params,
        "validated_export": operation == "mesh_export_stl",
    }


if __name__ == "__main__":
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (4 * 1024**3, 4 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (240, 240))
    resource.setrlimit(resource.RLIMIT_FSIZE, (80 * 1024**2, 80 * 1024**2))
    try:
        print(
            json.dumps(
                process(
                    Path(sys.argv[1]),
                    Path(sys.argv[2]),
                    sys.argv[3],
                    json.loads(sys.argv[4]),
                ),
                allow_nan=False,
            )
        )
    except Exception as exc:
        print(json.dumps({"error": str(exc)[:300]}))
        raise SystemExit(1) from None
