"""Workflow reuse, artifact handoffs and fail-closed execution boundaries."""

import io
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from openjarvis.artifacts.store import ArtifactNotFound, ArtifactStore
from openjarvis.server.workflows_router import templates
from openjarvis.workflow.jobs import Definition, ImageInstance, JobsStore, WorkflowJobs
from openjarvis.workflow.mesh_worker import check_glb, load, process

np = pytest.importorskip("numpy")
trimesh = pytest.importorskip("trimesh")


@pytest.fixture
def service(tmp_path, monkeypatch):
    instance = WorkflowJobs(ArtifactStore(tmp_path / "files"))
    monkeypatch.setattr(instance, "start_worker", lambda: None)
    yield instance
    instance.close()


def execute(service, run):
    with service.store.db() as db:
        row = dict(db.execute("SELECT * FROM runs WHERE id=?", (run["id"],)).fetchone())
    service._execute(row)
    return service.store.run(row["owner"], run["id"])


def mesh_definition(service):
    row = service.store.save("alice", templates()[0])
    for step in row["definition"]["steps"]:
        service.approve("admin", step["operation"], True)
    return row


def test_real_glb_pipeline_preserves_original_and_reuses_saved_workflow(service):
    data = trimesh.creation.box(extents=[1, 2, 3]).export(file_type="glb")
    original = service.save_bytes("alice", "original.glb", data)
    definition = mesh_definition(service)
    run = execute(
        service,
        service.submit(
            "alice",
            definition["id"],
            1,
            {
                "artifact_id": original["id"],
            },
        ),
    )
    assert run["status"] == "completed", run.get("error")
    assert [step["status"] for step in run["steps"]] == ["completed"] * 4
    output = run["output"]
    _, stl = service.artifacts.read("alice", output["artifact_id"])
    final = trimesh.load(io.BytesIO(stl), file_type="stl")
    assert np.allclose(final.extents, [100 / 3, 200 / 3, 100])
    assert final.is_watertight
    assert output["report"]["validated_export"] is True
    assert output["report"]["after"]["printability_verified"] is False
    assert service.artifacts.read("alice", original["id"])[1] == data
    for step in run["steps"]:
        report_id = step["output"]["report_artifact"]["id"]
        report = json.loads(service.artifacts.read("alice", report_id)[1])
        assert report["operation"] == step["operation"]
    definition["definition"]["steps"][2]["parameters"]["target_mm"] = 60.0
    updated = service.store.save("alice", definition["definition"], definition["id"], 1)
    second = execute(
        service,
        service.submit(
            "alice",
            updated["id"],
            2,
            {
                "artifact_id": original["id"],
            },
        ),
    )
    assert second["status"] == "completed"
    assert second["output"]["report"]["after"]["dimensions"] == [20, 40, 60]
    assert run["definition"]["steps"][2]["parameters"]["target_mm"] == 100


def test_glb_scene_transforms_and_components_are_preserved():
    scene = trimesh.Scene()
    scene.add_geometry(trimesh.creation.box(), node_name="one")
    transform = np.eye(4)
    transform[0, 3] = 5
    scene.add_geometry(trimesh.creation.box(), node_name="two", transform=transform)
    mesh = load(scene.export(file_type="glb"), "glb")
    assert mesh.extents[0] == 6
    assert len(mesh.split()) == 2


def test_basic_hole_repair_and_stl_watertight_requirement(tmp_path):
    mesh = trimesh.creation.box()
    mesh.update_faces(np.arange(len(mesh.faces)) != 0)
    original = tmp_path / "hole.glb"
    original.write_bytes(mesh.export(file_type="glb"))
    output = tmp_path / "repaired.ply"
    report = process(original, output, "mesh_repair", {"fill_small_holes": True})
    assert report["before"]["watertight"] is False
    assert report["after"]["watertight"] is True
    with pytest.raises(ValueError, match="not watertight"):
        process(original, tmp_path / "bad.stl", "mesh_export_stl", {})


def test_operation_approval_and_configured_capabilities_are_both_required(service):
    original = service.save_bytes("alice", "hello.txt", b"hello")
    definition = service.store.save(
        "alice",
        {
            "version": 1,
            "name": "Copy",
            "steps": [
                {"id": "copy", "operation": "artifact_copy"},
            ],
        },
    )
    with pytest.raises(ValueError, match="approval"):
        service.submit("alice", definition["id"], 1, {"artifact_id": original["id"]})
    service.approve("admin", "artifact_copy", True)
    service.parent_policy = SimpleNamespace(check=lambda *args: False)
    run = execute(
        service,
        service.submit(
            "alice",
            definition["id"],
            1,
            {
                "artifact_id": original["id"],
            },
        ),
    )
    assert run["status"] == "failed"
    assert "denied" in run["error"]
    assert len(service.artifacts.list("alice")) == 1


def test_revocation_between_steps_stops_next_operation(service, monkeypatch):
    original = service.save_bytes("alice", "hello.txt", b"hello")
    definition = service.store.save(
        "alice",
        {
            "version": 1,
            "name": "Copy twice",
            "steps": [
                {"id": "first", "operation": "artifact_copy"},
                {"id": "second", "operation": "artifact_copy", "input": "first"},
            ],
        },
    )
    service.approve("admin", "artifact_copy", True)
    perform = service.perform

    def revoke(*args):
        result = perform(*args)
        service.approve("admin", "artifact_copy", False)
        return result

    monkeypatch.setattr(service, "perform", revoke)
    run = execute(
        service,
        service.submit(
            "alice",
            definition["id"],
            1,
            {
                "artifact_id": original["id"],
            },
        ),
    )
    assert run["status"] == "failed"
    assert run["steps"][0]["status"] == "completed"
    assert run["steps"][1]["status"] == "failed"


def test_owner_and_revision_boundaries(service):
    original = service.save_bytes("alice", "hello.txt", b"hello")
    definition = mesh_definition(service)
    assert service.store.get_definition("bob", definition["id"]) is None
    with pytest.raises(KeyError):
        service.submit("bob", definition["id"], 1, {})
    with pytest.raises(ValueError, match="changed"):
        service.submit("alice", definition["id"], 2, {})
    with pytest.raises(ArtifactNotFound):
        service.perform("bob", "artifact_copy", {"artifact_id": original["id"]}, {})
    with pytest.raises(ValueError, match="changed"):
        service.store.save("alice", definition["definition"], definition["id"], 2)


def test_failed_step_preserves_reports_and_does_not_execute_successors(service):
    original = service.save_bytes("alice", "not-a-mesh.glb", b"bad")
    definition = mesh_definition(service)
    run = execute(
        service,
        service.submit(
            "alice",
            definition["id"],
            1,
            {
                "artifact_id": original["id"],
            },
        ),
    )
    assert run["status"] == "failed"
    assert len(run["steps"]) == 1
    assert service.artifacts.read("alice", original["id"])[1] == b"bad"


def test_stl_export_requires_explicit_units(service):
    artifact = service.save_bytes(
        "alice", "cube.glb", trimesh.creation.box().export(file_type="glb")
    )
    with pytest.raises(ValueError, match="dimension"):
        service.perform("alice", "mesh_export_stl", {"artifact_id": artifact["id"]}, {})


@pytest.mark.parametrize(
    "steps",
    [
        [{"id": "a", "operation": "shell_exec"}],
        [{"id": "a", "operation": "mesh_inspect", "input": "later"}],
        [{"id": "a", "operation": "mesh_inspect"}] * 2,
        [{"id": "a", "operation": "mesh_scale", "parameters": {"target_mm": -1}}],
        [{"id": "a", "operation": "mesh_repair", "parameters": {"script": "bad"}}],
        [
            {
                "id": "a",
                "operation": "mesh_blender_remesh",
                "parameters": {"voxel_size": float("nan")},
            }
        ],
    ],
)
def test_definition_validation_rejects_unsafe_or_invalid_steps(steps):
    with pytest.raises(ValidationError):
        Definition.model_validate({"version": 1, "name": "invalid", "steps": steps})


def test_restart_retains_completed_steps_and_marks_interrupted_run(service):
    definition = mesh_definition(service)
    run = service.submit("alice", definition["id"], 1, {})
    service.store.update(
        run["id"], "running", {"steps": [{"id": "inspect", "status": "completed"}]}
    )
    restarted = JobsStore(service.store.path)
    retained = restarted.run("alice", run["id"])
    assert retained["status"] == "failed"
    assert retained["steps"][0]["status"] == "completed"
    assert "restarted" in retained["error"]


def test_cancel_before_execution_and_disabled_owner(service):
    definition = mesh_definition(service)
    run = service.submit("alice", definition["id"], 1, {})
    with service.store.db() as db:
        db.execute("UPDATE runs SET cancel=1 WHERE id=?", (run["id"],))
    assert execute(service, run)["status"] == "cancelled"
    service.owner_check = lambda owner: False
    with pytest.raises(ValueError, match="disabled"):
        service.submit("alice", definition["id"], 1, {})


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://127.0.0.1:80",
        "http://localhost:8188",
        "http://127.0.0.1:8188/path",
    ],
)
def test_image_instances_reject_non_loopback_roots(url):
    with pytest.raises(ValidationError):
        ImageInstance(name="test", url=url, checkpoint="model.safetensors")


def test_instance_edits_disable_and_revisions_are_checked(service):
    values = {
        "name": "local",
        "url": "http://127.0.0.1:8188",
        "checkpoint": "model.safetensors",
    }
    instance = service.save_instance("admin", values)
    with pytest.raises(ValueError, match="disabled"):
        service.instance(instance["id"])
    with service.store.db() as db:
        db.execute("UPDATE instances SET enabled=1 WHERE id=?", (instance["id"],))
    assert service.instance(instance["id"]).name == "local"
    changed = service.save_instance(
        "admin", {**values, "name": "changed"}, instance["id"], 1
    )
    assert changed["revision"] == 2 and not changed["enabled"]
    with pytest.raises(ValueError, match="changed"):
        service.save_instance("admin", values, instance["id"], 1)


def test_automatic_completed_hunyuan_dispatches_once(service, monkeypatch):
    service.hunyuan = SimpleNamespace(
        own=lambda *args: None, status=lambda *args: {"status": "completed"}
    )
    original = service.save_bytes(
        "alice", "cube.glb", trimesh.creation.box().export(file_type="glb")
    )
    definition = mesh_definition(service)
    monkeypatch.setattr(service, "import_hunyuan", lambda *args: original)
    service.subscribe("alice", "job", definition["id"], 1)
    service.poll_triggers()
    service.poll_triggers()
    triggers = service.triggers("alice")
    assert triggers[0]["state"] == "submitted"
    assert service.store.run("alice", triggers[0]["run_id"])["status"] == "queued"
    assert not service.triggers("bob")
    with service.store.db() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_external_glb_resource_is_rejected_before_loading():
    document = json.dumps(
        {"asset": {"version": "2.0"}, "buffers": [{"uri": "/etc/passwd"}]}
    ).encode()
    document += b" " * (-len(document) % 4)
    import struct

    data = (
        struct.pack("<III", 0x46546C67, 2, 20 + len(document))
        + struct.pack("<II", len(document), 0x4E4F534A)
        + document
    )
    with pytest.raises(ValueError, match="external URIs"):
        check_glb(data)


def test_enabled_capability_enforcement_requires_parent_policy(service):
    original = service.save_bytes("alice", "input.txt", b"hello")
    definition = service.store.save(
        "alice",
        {
            "version": 1,
            "name": "Copy",
            "steps": [{"id": "copy", "operation": "artifact_copy"}],
        },
    )
    service.approve("admin", "artifact_copy", True)
    service.config = SimpleNamespace(
        security=SimpleNamespace(capabilities=SimpleNamespace(enabled=True))
    )
    run = execute(
        service,
        service.submit("alice", definition["id"], 1, {"artifact_id": original["id"]}),
    )
    assert run["status"] == "failed"
    assert len(service.artifacts.list("alice")) == 1


def test_optional_blender_dependency_failure_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    source = tmp_path / "cube.glb"
    source.write_bytes(trimesh.creation.box().export(file_type="glb"))
    with pytest.raises(ValueError, match="Install Blender"):
        process(
            source, tmp_path / "out.ply", "mesh_blender_remesh", {"voxel_size": 0.1}
        )
