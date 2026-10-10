"""Blender reads only numeric geometry written by the mesh worker, never scripts."""

import json
import sys
from pathlib import Path


def main():
    import bpy

    source, destination = sys.argv[sys.argv.index("--") + 1 :]
    data = json.loads(Path(source).read_text())
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    mesh = bpy.data.meshes.new("workflow_mesh")
    mesh.from_pydata(data["vertices"], [], data["faces"])
    mesh.update()
    obj = bpy.data.objects.new("workflow_object", mesh)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    modifier = obj.modifiers.new("workflow_voxel_remesh", "REMESH")
    modifier.mode = "VOXEL"
    modifier.voxel_size = data["voxel_size"]
    bpy.ops.object.modifier_apply(modifier=modifier.name)
    obj.data.calc_loop_triangles()
    if len(obj.data.loop_triangles) > 400_000 or len(obj.data.vertices) > 800_000:
        raise ValueError("Remeshed geometry exceeds limits")
    Path(destination).write_text(
        json.dumps(
            {
                "vertices": [list(vertex.co) for vertex in obj.data.vertices],
                "faces": [list(face.vertices) for face in obj.data.loop_triangles],
            }
        )
    )


if __name__ == "__main__":
    main()
