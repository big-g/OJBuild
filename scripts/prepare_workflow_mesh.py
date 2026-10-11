#!/usr/bin/env python3
"""Prepare a separate, bounded GLB copy for the Phase 3 mesh workflow."""

import argparse
import io
import json
import os
import struct
from pathlib import Path

INPUT_BYTES = 64 * 1024 * 1024
OUTPUT_BYTES = 20 * 1024 * 1024
INPUT_FACES = 2_000_000
INPUT_VERTICES = 1_000_000


def prepare(source: Path, destination: Path, faces: int = 300_000) -> dict:
    import numpy as np
    import trimesh

    if not 100 <= faces <= 400_000:
        raise ValueError("Target faces must be between 100 and 400000")
    if source.resolve() == destination.resolve() or destination.exists():
        raise ValueError(
            "Choose a new output path; existing files are never overwritten"
        )
    with source.open("rb") as stream:
        data = stream.read(INPUT_BYTES + 1)
    if len(data) > INPUT_BYTES:
        raise ValueError("Input exceeds 64 MiB")
    if len(data) < 20:
        raise ValueError("Input must be a self-contained binary GLB")
    magic, version, total, size, kind = struct.unpack("<4sIIII", data[:20])
    if (magic, version, total, kind) != (b"glTF", 2, len(data), 0x4E4F534A):
        raise ValueError("Invalid GLB header")
    if size > len(data) - 20:
        raise ValueError("Invalid GLB JSON chunk")
    document = json.loads(data[20 : 20 + size])
    for item in document.get("buffers", []) + document.get("images", []):
        if "uri" in item:
            raise ValueError("External buffers/images are not allowed")
    for accessor in document.get("accessors", []):
        count = accessor.get("count")
        if type(count) is not int or not 0 <= count <= INPUT_FACES * 3:
            raise ValueError("GLB accessor exceeds the input budget")
    scene = trimesh.load(
        io.BytesIO(data), file_type="glb", force="scene", skip_materials=True
    )
    nodes = scene.graph.nodes_geometry
    if len(scene.geometry) > 128 or len(nodes) > 128:
        raise ValueError("Input exceeds 128 geometries/instances")
    face_count = vertex_count = 0
    for node in nodes:
        _, name = scene.graph[node]
        geometry = scene.geometry[name]
        if not isinstance(geometry, trimesh.Trimesh):
            raise ValueError("Input contains non-mesh geometry")
        face_count += len(geometry.faces)
        vertex_count += len(geometry.vertices)
    if face_count > INPUT_FACES or vertex_count > INPUT_VERTICES:
        raise ValueError("Input exceeds 2000000 faces or 1000000 vertices")
    mesh = scene.to_mesh()
    if not len(mesh.faces) or not np.isfinite(mesh.vertices).all():
        raise ValueError("Input mesh is empty or has non-finite coordinates")

    def summary(value):
        return {
            "vertices": len(value.vertices),
            "faces": len(value.faces),
            "bounds": value.bounds.tolist(),
            "watertight": bool(value.is_watertight),
        }

    before = summary(mesh)
    if len(mesh.faces) > faces:
        mesh = mesh.simplify_quadric_decimation(face_count=faces)
    mesh.remove_unreferenced_vertices()
    if (
        not len(mesh.faces)
        or len(mesh.faces) > faces
        or len(mesh.vertices) > 800_000
        or not np.isfinite(mesh.vertices).all()
    ):
        raise ValueError("Simplification did not produce a mesh within workflow limits")
    output = mesh.export(file_type="glb")
    if len(output) > OUTPUT_BYTES:
        raise ValueError("Prepared GLB exceeds the workflow's 20 MiB file limit")
    reloaded = trimesh.load(
        io.BytesIO(output), file_type="glb", force="scene"
    ).to_mesh()
    if (
        len(reloaded.faces) != len(mesh.faces)
        or not np.isfinite(reloaded.vertices).all()
        or not np.allclose(reloaded.bounds, mesh.bounds)
    ):
        raise ValueError("Prepared GLB failed export/reload validation")
    after = summary(reloaded)
    # Exclusive creation also protects against a path appearing during processing.
    fd = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(output)
    except BaseException:
        destination.unlink()
        raise
    return {
        "source": str(source),
        "output": str(destination),
        "bytes": len(output),
        "before": before,
        "after": after,
        "review": "Simplification may change detail/topology. Review the copy; "
        "inspect, repair and explicitly scale before STL export.",
    }


def main():
    import resource

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--faces", type=int, default=300_000)
    args = parser.parse_args()
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "1"
    resource.setrlimit(resource.RLIMIT_AS, (4 * 1024**3, 4 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (240, 240))
    resource.setrlimit(resource.RLIMIT_FSIZE, (OUTPUT_BYTES, OUTPUT_BYTES))
    try:
        result = prepare(args.source, args.destination, args.faces)
    except Exception as exc:
        parser.exit(1, f"Preparation failed: {exc}\n")
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
