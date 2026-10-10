"""HTTP ownership, role checks, configuration revisions and job visibility."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.artifacts.store import ArtifactStore
from openjarvis.server.auth_store import AuthStore
from openjarvis.server.workflows_router import create_workflows_router
from openjarvis.workflow.jobs import WorkflowJobs


@pytest.fixture
def clients(tmp_path, monkeypatch):
    app = FastAPI()
    app.state.auth_store = AuthStore(tmp_path / "auth.db")
    service = WorkflowJobs(ArtifactStore(tmp_path / "files"))
    monkeypatch.setattr(service, "start_worker", lambda: None)
    app.include_router(create_workflows_router(service))
    rows = []
    for name, admin in (("alice", False), ("bob", False), ("admin", True)):
        app.state.auth_store.create_user(name, name, "password", is_admin=admin)
        token = app.state.auth_store.create_session(name)
        rows.append(TestClient(app, headers={"X-OpenJarvis-Session": token}))
    yield (*rows, service, app)
    service.close()


DEFINITION = {
    "version": 1,
    "name": "Copy",
    "steps": [{"id": "copy", "operation": "artifact_copy"}],
}


def test_authentication_and_administrator_permissions(clients):
    alice, _, admin, _, app = clients
    assert TestClient(app).get("/v1/workflows").status_code == 401
    assert alice.get("/v1/workflows/admin").status_code == 403
    assert (
        alice.post(
            "/v1/workflows/admin/operations/artifact_copy", json={"enabled": True}
        ).status_code
        == 403
    )
    assert (
        admin.post(
            "/v1/workflows/admin/operations/artifact_copy", json={"enabled": True}
        ).status_code
        == 200
    )
    assert (
        admin.post(
            "/v1/workflows/admin/operations/artifact_copy", json={"enabled": "true"}
        ).status_code
        == 400
    )


def test_workflow_and_run_ownership_including_admin(clients):
    alice, bob, admin, service, _ = clients
    saved = alice.post("/v1/workflows", json={"definition": DEFINITION}).json()
    artifact = service.save_bytes("alice", "input.txt", b"hello")
    admin.post("/v1/workflows/admin/operations/artifact_copy", json={"enabled": True})
    payload = {
        "workflow_id": saved["id"],
        "revision": 1,
        "input": {"artifact_id": artifact["id"]},
    }
    assert bob.post("/v1/workflows/runs", json=payload).status_code == 404
    assert admin.post("/v1/workflows/runs", json=payload).status_code == 404
    response = alice.post("/v1/workflows/runs", json=payload)
    assert response.status_code == 202
    run_id = response.json()["id"]
    for stranger in (bob, admin):
        assert stranger.get("/v1/workflows/runs/" + run_id).status_code == 404
        assert stranger.get("/v1/workflows").json()["workflows"] == []
    assert alice.post(f"/v1/workflows/runs/{run_id}/cancel").status_code == 200
    assert alice.get(f"/v1/workflows/runs/{run_id}").json()["cancel_requested"] is True


def test_definition_revisions_and_invalid_requests(clients):
    alice, _, _, _, _ = clients
    saved = alice.post("/v1/workflows", json={"definition": DEFINITION}).json()
    updated = alice.post(
        "/v1/workflows", json={**saved, "definition": {**DEFINITION, "name": "New"}}
    )
    assert updated.status_code == 201
    assert updated.json()["revision"] == 2
    assert alice.post("/v1/workflows", json=saved).status_code == 400
    assert (
        alice.post(
            "/v1/workflows/runs",
            json={"workflow_id": saved["id"], "revision": True, "input": {}},
        ).status_code
        == 400
    )
    assert (
        alice.post(
            "/v1/workflows", json={"definition": DEFINITION, "id": []}
        ).status_code
        == 400
    )
    assert alice.post("/v1/workflows", content="{" + "x" * 65536).status_code == 413
    assert alice.delete(f"/v1/workflows/{saved['id']}?revision=1").status_code == 409
    assert alice.delete(f"/v1/workflows/{saved['id']}?revision=2").status_code == 200


def test_image_server_configuration_is_disabled_until_verified(clients, monkeypatch):
    alice, _, admin, service, _ = clients
    config = {
        "name": "Image server",
        "url": "http://127.0.0.1:8188",
        "checkpoint": "test.safetensors",
    }
    assert (
        alice.post(
            "/v1/workflows/admin/image-servers", json={"config": config}
        ).status_code
        == 403
    )
    saved = admin.post(
        "/v1/workflows/admin/image-servers", json={"config": config}
    ).json()
    assert not saved["enabled"]
    monkeypatch.setattr(
        "openjarvis.workflow.providers.request",
        lambda *args, **kwargs: {
            "CheckpointLoaderSimple": {
                "input": {"required": {"ckpt_name": [["test.safetensors"]]}}
            },
        },
    )
    assert (
        admin.post(
            f"/v1/workflows/admin/image-servers/{saved['id']}/enable",
            json={"revision": 1, "enabled": True},
        ).status_code
        == 200
    )
    assert service.instance(saved["id"]).checkpoint == config["checkpoint"]
    public = alice.get("/v1/workflows").json()["image_servers"][0]
    assert "url" not in public and "checkpoint" not in public
    changed = admin.post(
        "/v1/workflows/admin/image-servers",
        json={
            "id": saved["id"],
            "revision": 1,
            "config": {**config, "name": "Changed"},
        },
    ).json()
    assert changed["revision"] == 2 and not changed["enabled"]
    assert (
        admin.post(
            f"/v1/workflows/admin/image-servers/{saved['id']}/enable",
            json={"revision": 1, "enabled": True},
        ).status_code
        == 409
    )


def test_role_revocation_during_provider_test_cannot_enable_server(
    clients, monkeypatch
):
    _, _, admin, service, app = clients
    saved = admin.post(
        "/v1/workflows/admin/image-servers",
        json={
            "config": {
                "name": "server",
                "url": "http://127.0.0.1:8188",
                "checkpoint": "test.safetensors",
            }
        },
    ).json()

    def revoke(*args, **kwargs):
        app.state.auth_store.set_admin("admin", False)
        return {
            "CheckpointLoaderSimple": {
                "input": {"required": {"ckpt_name": [["test.safetensors"]]}}
            }
        }

    monkeypatch.setattr("openjarvis.workflow.providers.request", revoke)
    response = admin.post(
        f"/v1/workflows/admin/image-servers/{saved['id']}/enable",
        json={"revision": 1, "enabled": True},
    )
    assert response.status_code == 403
    with pytest.raises(ValueError, match="disabled"):
        service.instance(saved["id"])


def test_server_acceptance_script_runs_real_saved_revisions(clients, monkeypatch):
    import runpy
    from pathlib import Path

    pytest.importorskip("trimesh")

    from openjarvis.server.artifacts_router import create_artifacts_router

    alice, _, _, service, app = clients
    app.include_router(create_artifacts_router(service.artifacts))
    monkeypatch.undo()
    for operation in ("mesh_inspect", "mesh_repair", "mesh_scale", "mesh_export_stl"):
        service.approve("admin", operation, True)
    script = Path(__file__).parents[2] / "scripts" / "check_phase3.py"
    checks = runpy.run_path(str(script))["checks"]
    run_ids = checks(alice, 30)
    assert len(run_ids) == 2
    assert all(
        service.store.run("alice", identity)["status"] == "completed"
        for identity in run_ids
    )


def test_attachment_cancellation_and_cleanup_are_owner_scoped(clients):
    alice, bob, _, service, _ = clients
    with service.store.db() as db:
        db.execute(
            "INSERT INTO triggers VALUES(?,?,?,?,?,'waiting',NULL,NULL)",
            ("trigger", "alice", "job", "workflow", 1),
        )
    assert bob.delete("/v1/workflows/after-3d/trigger").status_code == 404
    with service.store.db() as db:
        db.execute("UPDATE triggers SET state='dispatching'")
    assert alice.delete("/v1/workflows/after-3d/trigger").status_code == 409
    with service.store.db() as db:
        db.execute("UPDATE triggers SET state='waiting'")
    assert alice.delete("/v1/workflows/after-3d/trigger").status_code == 200
    assert service.triggers("alice") == []
