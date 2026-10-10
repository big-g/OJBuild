"""Authenticated workflow definitions, jobs, operation approvals and image servers."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, ValidationError

from openjarvis.artifacts.store import ArtifactError, ArtifactNotFound
from openjarvis.server.auth import authenticate_admin_request, authenticate_request
from openjarvis.workflow.jobs import StrictModel


class SaveRequest(StrictModel):
    definition: dict
    id: str | None = Field(default=None, max_length=36)
    revision: int | None = Field(default=None, ge=1)


class RunRequest(StrictModel):
    workflow_id: str = Field(max_length=36)
    revision: int = Field(ge=1)
    input: dict


class InstanceRequest(StrictModel):
    config: dict
    id: str | None = Field(default=None, max_length=36)
    revision: int | None = Field(default=None, ge=1)


class EnableRequest(StrictModel):
    revision: int = Field(ge=1)
    enabled: bool


class SubscribeRequest(StrictModel):
    job_id: str = Field(max_length=36)
    workflow_id: str = Field(max_length=36)
    revision: int = Field(ge=1)


async def body(request, limit=64 * 1024):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise HTTPException(413, "Workflow request is too large")
        data.extend(chunk)
    try:
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except ValueError:
        raise HTTPException(400, "Expected a JSON object") from None


def failure(exc):
    if isinstance(exc, (KeyError, ArtifactNotFound)):
        return HTTPException(404, "Workflow, run or file not found")
    if isinstance(exc, ValidationError):
        messages = [
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
            for e in exc.errors(include_input=False)
        ]
        return HTTPException(400, "; ".join(messages)[:500])
    return HTTPException(400, str(exc)[:500])


def templates():
    mesh = [
        {"id": "inspect", "operation": "mesh_inspect", "input": "input"},
        {
            "id": "repair",
            "operation": "mesh_repair",
            "input": "inspect",
            "parameters": {"fill_small_holes": True, "keep_largest": False},
        },
        {
            "id": "scale",
            "operation": "mesh_scale",
            "input": "repair",
            "parameters": {"target_mm": 100.0, "axis": "longest"},
        },
        {
            "id": "export",
            "operation": "mesh_export_stl",
            "input": "scale",
            "parameters": {"require_watertight": True},
        },
    ]
    # Image server instance is selected by the user before saving/running.
    image = {
        "id": "image",
        "operation": "image_generate_local",
        "input": "input",
        "parameters": {
            "instance_id": "",
            "width": 512,
            "height": 512,
            "steps": 20,
            "seed": 42,
            "negative_prompt": "",
        },
    }
    return [
        {"version": 1, "name": "GLB inspection, repair and STL", "steps": mesh},
        {"version": 1, "name": "Text to image", "steps": [image]},
        {
            "version": 1,
            "name": "Text to 3D and STL",
            "steps": [
                image,
                {
                    "id": "generate",
                    "operation": "hunyuan_generate",
                    "input": "image",
                    "parameters": {"model": "turbo"},
                },
                *[
                    {
                        **step,
                        "input": "generate"
                        if step["input"] == "input"
                        else step["input"],
                    }
                    for step in mesh
                ],
            ],
        },
    ]


def create_workflows_router(service):
    router = APIRouter(prefix="/v1/workflows", tags=["workflows"])

    def available(owner=Depends(authenticate_request)):
        if service is None:
            raise HTTPException(503, "Workflow storage is unavailable")
        return owner

    def admin(actor=Depends(authenticate_admin_request)):
        if service is None:
            raise HTTPException(503, "Workflow storage is unavailable")
        return actor

    @router.get("")
    def list_workflows(owner=Depends(available)):
        return {
            "workflows": service.store.definitions(owner),
            "operations": service.operations(),
            "templates": templates(),
            "image_servers": service.instances(),
        }

    @router.post("", status_code=201)
    async def save_workflow(request: Request, owner=Depends(available)):
        values = await body(request)
        try:
            values = SaveRequest.model_validate(values).model_dump(exclude_none=True)
            if set(values) - {"id", "revision", "definition"}:
                raise ValueError("Expected definition, optional id and revision")
            return service.store.save(
                owner,
                values.get("definition"),
                values.get("id"),
                values.get("revision"),
            )
        except (ValueError, KeyError) as exc:
            raise failure(exc) from None

    @router.delete("/{workflow_id}")
    def delete_workflow(workflow_id: str, revision: int, owner=Depends(available)):
        with service.store.db() as db:
            if not db.execute(
                "DELETE FROM definitions WHERE id=? AND owner=? AND revision=?",
                (workflow_id, owner, revision),
            ).rowcount:
                raise HTTPException(409, "Workflow missing or changed; reload")
            service.store.log(db, owner, "delete_workflow", workflow_id)
        return {"deleted": True}

    @router.post("/runs", status_code=202)
    async def submit(request: Request, owner=Depends(available)):
        values = await body(request)
        try:
            values = RunRequest.model_validate(values).model_dump()
            if set(values) != {"workflow_id", "revision", "input"} or not isinstance(
                values["input"], dict
            ):
                raise ValueError("Expected workflow_id, revision and input object")
            return service.submit(
                owner, values["workflow_id"], values["revision"], values["input"]
            )
        except (ValueError, KeyError, ArtifactError) as exc:
            raise failure(exc) from None

    @router.get("/runs")
    def list_runs(owner=Depends(available)):
        with service.store.db() as db:
            identities = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM runs WHERE owner=? ORDER BY created DESC LIMIT 100",
                    (owner,),
                )
            ]
        return {"runs": [service.store.run(owner, identity) for identity in identities]}

    @router.get("/runs/{run_id}")
    def get_run(run_id: str, owner=Depends(available)):
        try:
            return service.store.run(owner, run_id)
        except KeyError as exc:
            raise failure(exc) from None

    @router.post("/runs/{run_id}/cancel")
    def cancel(run_id: str, owner=Depends(available)):
        try:
            run = service.store.run(owner, run_id)
        except KeyError as exc:
            raise failure(exc) from None
        if run["status"] not in {"queued", "running"}:
            raise HTTPException(409, "Run is already finished")
        with service.store.db() as db:
            db.execute(
                "UPDATE runs SET cancel=1 WHERE id=? AND owner=?", (run_id, owner)
            )
            service.store.log(db, owner, "cancel_run", run_id)
        return {"cancel_requested": True}

    @router.delete("/runs/{run_id}")
    def delete_run(run_id: str, owner=Depends(available)):
        with service.store.db() as db:
            if not db.execute(
                "DELETE FROM runs WHERE id=? AND owner=? "
                "AND state NOT IN ('queued','running')",
                (run_id, owner),
            ).rowcount:
                raise HTTPException(409, "Run missing or still active")
            service.store.log(db, owner, "delete_run", run_id)
        return {"deleted": True}

    @router.post("/import-3d/{job_id}", status_code=201)
    def import_mesh(job_id: str, owner=Depends(available)):
        if service.hunyuan is None:
            raise HTTPException(503, "Hunyuan is not configured")
        try:
            return service.import_hunyuan(owner, job_id)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "3D output could not be imported") from None

    @router.get("/after-3d")
    def triggers(owner=Depends(available)):
        return {"triggers": service.triggers(owner)}

    @router.delete("/after-3d/{trigger_id}")
    def remove_trigger(trigger_id: str, owner=Depends(available)):
        with service.store.db() as db:
            row = db.execute(
                "SELECT state FROM triggers WHERE id=? AND owner=?",
                (trigger_id, owner),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Automatic workflow not found")
            if not db.execute(
                "DELETE FROM triggers WHERE id=? AND owner=? AND state!='dispatching'",
                (trigger_id, owner),
            ).rowcount:
                raise HTTPException(409, "Workflow is dispatching; try again shortly")
            service.store.log(db, owner, "remove_3d_workflow", trigger_id)
        return {"deleted": True}

    @router.post("/after-3d", status_code=202)
    async def subscribe(request: Request, owner=Depends(available)):
        values = await body(request)
        if set(values) != {"job_id", "workflow_id", "revision"}:
            raise HTTPException(400, "Expected job_id, workflow_id and revision")
        try:
            values = SubscribeRequest.model_validate(values).model_dump()
            service.subscribe(
                owner, values["job_id"], values["workflow_id"], values["revision"]
            )
            return {"triggers": service.triggers(owner)}
        except (ValueError, KeyError) as exc:
            raise failure(exc) from None

    @router.get("/admin")
    def configuration(actor=Depends(admin)):
        return {
            "operations": service.operations(),
            "image_servers": service.instances(admin=True),
        }

    @router.post("/admin/operations/{operation}")
    async def approve(operation: str, request: Request, actor=Depends(admin)):
        values = await body(request)
        actor = authenticate_admin_request(request)
        if set(values) != {"enabled"} or not isinstance(values["enabled"], bool):
            raise HTTPException(400, "Expected enabled: true or false")
        try:
            service.approve(actor, operation, values["enabled"])
            return {"operations": service.operations()}
        except ValueError as exc:
            raise failure(exc) from None

    @router.post("/admin/image-servers", status_code=201)
    async def configure_image(request: Request, actor=Depends(admin)):
        values = await body(request)
        actor = authenticate_admin_request(request)
        try:
            values = InstanceRequest.model_validate(values).model_dump(
                exclude_none=True
            )
            if set(values) - {"id", "revision", "config"}:
                raise ValueError("Expected config, optional id and revision")
            return service.save_instance(
                actor, values.get("config"), values.get("id"), values.get("revision")
            )
        except ValueError as exc:
            raise failure(exc) from None

    @router.post("/admin/image-servers/{instance_id}/enable")
    async def enable_image(instance_id: str, request: Request, actor=Depends(admin)):
        values = await body(request)
        try:
            values = EnableRequest.model_validate(values).model_dump()
        except ValueError as exc:
            raise failure(exc) from None
        if set(values) != {"revision", "enabled"} or not isinstance(
            values["enabled"], bool
        ):
            raise HTTPException(400, "Expected revision and enabled")
        with service.store.db() as db:
            row = db.execute(
                "SELECT * FROM instances WHERE id=? AND revision=?",
                (instance_id, values["revision"]),
            ).fetchone()
        if not row:
            raise HTTPException(409, "Image server changed; reload")
        if values["enabled"]:
            from openjarvis.workflow.providers import request as local_request

            config = json.loads(row["config"])
            try:
                manifest = local_request(
                    config["url"], "/object_info/CheckpointLoaderSimple"
                )
                checkpoints = manifest["CheckpointLoaderSimple"]["input"]["required"][
                    "ckpt_name"
                ][0]
                if config["checkpoint"] not in checkpoints:
                    raise ValueError
            except Exception:
                raise HTTPException(
                    400,
                    "Cannot verify checkpoint. Start ComfyUI and install "
                    "the named SD/SDXL checkpoint",
                ) from None
        actor = authenticate_admin_request(request)
        with service.store.db() as db:
            if not db.execute(
                "UPDATE instances SET enabled=? WHERE id=? AND revision=?",
                (int(values["enabled"]), instance_id, values["revision"]),
            ).rowcount:
                raise HTTPException(409, "Image server changed; reload")
            service.store.log(
                db,
                actor,
                "enable_image_server" if values["enabled"] else "disable_image_server",
                instance_id,
            )
        return {"image_servers": service.instances(admin=True)}

    @router.delete("/admin/image-servers/{instance_id}")
    def delete_image(instance_id: str, revision: int, actor=Depends(admin)):
        with service.store.db() as db:
            if not db.execute(
                "DELETE FROM instances WHERE id=? AND revision=?",
                (instance_id, revision),
            ).rowcount:
                raise HTTPException(409, "Image server missing or changed; reload")
            service.store.log(db, actor, "delete_image_server", instance_id)
        return {"deleted": True}

    return router
