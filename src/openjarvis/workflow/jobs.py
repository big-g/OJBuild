"""Persistent owner-scoped ordered workflows over approved tool operations.

This service deliberately exposes no expressions, shell commands or arbitrary
tool names. Operations are versioned adapters and every step uses ToolExecutor.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openjarvis.artifacts.store import ArtifactError, ArtifactStore, _directory
from openjarvis.core.correlation import ExecutionIdentity, execution_scope
from openjarvis.core.types import ToolCall, ToolResult
from openjarvis.security.capabilities import resolve_declared_tool_capabilities
from openjarvis.security.capability_registry import (
    ApprovalRecord,
    Provenance,
    create_builtin_capability_registry,
)
from openjarvis.security.tool_management_bootstrap import sync_managed_tool
from openjarvis.security.tool_management_registry import ToolManagementRegistry
from openjarvis.tools._stubs import BaseTool, ToolExecutor, ToolSpec

logger = logging.getLogger(__name__)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EmptyParams(StrictModel):
    pass


class RepairParams(StrictModel):
    fill_small_holes: bool = True
    keep_largest: bool = False


class ScaleParams(StrictModel):
    target_mm: float = Field(ge=1, le=3000, allow_inf_nan=False)
    axis: Literal["longest", "x", "y", "z"] = "longest"


class BlenderParams(StrictModel):
    voxel_size: float = Field(ge=0.001, le=100, allow_inf_nan=False)


class ExportParams(StrictModel):
    require_watertight: bool = True


class ImageParams(StrictModel):
    instance_id: str
    width: int = Field(default=512, ge=256, le=1024, multiple_of=64)
    height: int = Field(default=512, ge=256, le=1024, multiple_of=64)
    steps: int = Field(default=20, ge=1, le=50)
    seed: int = Field(default=42, ge=0, le=2**32 - 1)
    negative_prompt: str = Field(default="", max_length=2000)


class HunyuanParams(StrictModel):
    model: Literal["turbo", "standard"] = "turbo"
    seed: int = Field(default=42, ge=0, le=2**32 - 1)


PARAMETERS = {
    "artifact_copy": EmptyParams,
    "mesh_inspect": EmptyParams,
    "mesh_repair": RepairParams,
    "mesh_scale": ScaleParams,
    "mesh_blender_remesh": BlenderParams,
    "mesh_export_stl": ExportParams,
    "image_generate_local": ImageParams,
    "hunyuan_generate": HunyuanParams,
}
DESCRIPTIONS = {
    "artifact_copy": "Copy an owned file, preserving its original",
    "mesh_inspect": "Report dimensions, components, winding and watertightness",
    "mesh_repair": "Clean duplicate/degenerate faces, fix normals and small holes",
    "mesh_scale": "Uniformly scale one dimension to millimetres",
    "mesh_blender_remesh": "Voxel remesh with headless Blender; may change detail",
    "mesh_export_stl": "Export and reload STL in millimetres; requires a scale step",
    "image_generate_local": "Generate an image using a configured local ComfyUI server",
    "hunyuan_generate": "Generate a GLB from an image using the local Hunyuan worker",
}


class Step(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    operation: str
    input: str = "input"
    parameters: dict = Field(default_factory=dict)


class Definition(StrictModel):
    version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=100)
    steps: list[Step] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def ordered(self):
        seen = {"input"}
        for step in self.steps:
            if step.id in seen or step.input not in seen:
                raise ValueError("Use unique step IDs and input or an earlier step ID")
            if step.operation not in PARAMETERS:
                raise ValueError("Unknown workflow operation")
            PARAMETERS[step.operation].model_validate(step.parameters)
            seen.add(step.id)
        return self


class ImageInstance(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(pattern=r"^http://127\.0\.0\.1:[0-9]{2,5}$")
    checkpoint: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def safe(self):
        from urllib.parse import urlsplit

        if not 1024 <= urlsplit(self.url).port <= 65535:
            raise ValueError("Use a loopback ComfyUI port between 1024 and 65535")
        if ".." in self.checkpoint or any(c in self.checkpoint for c in "/\\\x00"):
            raise ValueError(
                "Use the installed checkpoint filename without directories"
            )
        return self


class JobsStore:
    def __init__(self, path):
        self.path = Path(path)
        with _directory(self.path.parent) as fd:
            handle = os.open(
                self.path.name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=fd
            )
            os.close(handle)
        with self.db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise ValueError("Unsupported workflow database version")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS definitions (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    revision INTEGER NOT NULL, definition TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, workflow_id TEXT,
                    revision INTEGER NOT NULL, snapshot TEXT NOT NULL,
                    input TEXT NOT NULL, state TEXT NOT NULL, result TEXT NOT NULL,
                    created REAL NOT NULL, cancel INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS approvals (
                    operation TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    actor TEXT NOT NULL, enabled INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS instances (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    config TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, actor TEXT NOT NULL,
                    action TEXT NOT NULL, resource TEXT NOT NULL,
                    created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS triggers (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, job_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    state TEXT NOT NULL, run_id TEXT, error TEXT,
                    UNIQUE(owner, job_id));
                PRAGMA user_version=1;
            """)
            interrupted = db.execute(
                "SELECT id,result FROM runs WHERE state IN ('queued','running')"
            ).fetchall()
            for row in interrupted:
                result = json.loads(row["result"])
                result["error"] = "Server restarted before run completed"
                db.execute(
                    "UPDATE runs SET state='failed',result=? WHERE id=?",
                    (json.dumps(result), row["id"]),
                )
            db.execute(
                "UPDATE triggers SET state='failed',"
                "error='Server restarted during dispatch' "
                "WHERE state='dispatching'"
            )

    @contextmanager
    def db(self):
        with _directory(self.path.parent):
            if self.path.is_symlink():
                raise ValueError("Unsafe workflow catalog")
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def log(self, db, actor, action, resource):
        db.execute(
            "INSERT INTO audit(actor,action,resource,created) VALUES(?,?,?,?)",
            (actor, action, resource, time.time()),
        )

    def definitions(self, owner):
        with self.db() as db:
            return [
                {
                    "id": r["id"],
                    "revision": r["revision"],
                    "definition": json.loads(r["definition"]),
                }
                for r in db.execute(
                    "SELECT * FROM definitions WHERE owner=? ORDER BY rowid", (owner,)
                )
            ]

    def get_definition(self, owner, identity):
        return next((r for r in self.definitions(owner) if r["id"] == identity), None)

    def save(self, owner, definition, identity=None, revision=None):
        definition = Definition.model_validate(definition).model_dump()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if identity:
                old = db.execute(
                    "SELECT * FROM definitions WHERE id=? AND owner=?",
                    (identity, owner),
                ).fetchone()
                if not old:
                    raise KeyError("Workflow not found")
                if revision != old["revision"]:
                    raise ValueError("Workflow changed; reload before saving")
                revision += 1
                db.execute(
                    "UPDATE definitions SET revision=?,definition=? WHERE id=?",
                    (revision, json.dumps(definition), identity),
                )
            else:
                if (
                    db.execute(
                        "SELECT count(*) FROM definitions WHERE owner=?", (owner,)
                    ).fetchone()[0]
                    >= 100
                ):
                    raise ValueError("Workflow limit reached (100 per account)")
                identity, revision = str(uuid.uuid4()), 1
                db.execute(
                    "INSERT INTO definitions VALUES(?,?,?,?)",
                    (identity, owner, revision, json.dumps(definition)),
                )
            self.log(db, owner, "save_workflow", identity)
        return {"id": identity, "revision": revision, "definition": definition}

    def run(self, owner, identity):
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM runs WHERE id=? AND owner=?", (identity, owner)
            ).fetchone()
        if not row:
            raise KeyError("Run not found")
        return {
            "id": row["id"],
            "workflow_id": row["workflow_id"],
            "revision": row["revision"],
            "status": row["state"],
            "definition": json.loads(row["snapshot"]),
            "input": json.loads(row["input"]),
            "created": row["created"],
            "cancel_requested": bool(row["cancel"]),
            **json.loads(row["result"]),
        }

    def update(self, identity, state, result):
        with self.db() as db:
            db.execute(
                "UPDATE runs SET state=?,result=? WHERE id=?",
                (state, json.dumps(result), identity),
            )


class OperationPolicy:
    """Explicit administrative operation grants intersect configured capabilities."""

    def __init__(self, service):
        self.service = service

    def resolve_effective_tool_capabilities(self, tool, params):
        return resolve_declared_tool_capabilities(tool.spec, self.service.capabilities)

    def check(self, agent_id, capability, resource):
        if not self.service.approved(resource):
            return False
        parent = self.service.parent_policy
        security = getattr(self.service.config, "security", None)
        if parent is None and getattr(
            getattr(security, "capabilities", None), "enabled", False
        ):
            return False
        return parent is None or parent.check(
            self.service.parent_agent, capability, resource
        )


class OperationTool(BaseTool):
    def __init__(self, service, operation, owner="", input_value=None):
        self.service, self.tool_id, self.owner = service, operation, owner
        self.input_value = input_value or {}

    @property
    def spec(self):
        capabilities = ["file:read", "file:write"]
        if self.tool_id in {"image_generate_local", "hunyuan_generate"}:
            capabilities.append("network:fetch")
        if self.tool_id == "mesh_blender_remesh":
            capabilities.append("code:execute")
        return ToolSpec(
            name=self.tool_id,
            description=DESCRIPTIONS[self.tool_id],
            parameters=PARAMETERS[self.tool_id].model_json_schema(),
            required_capabilities=capabilities,
            timeout_seconds=1000,
            metadata={"operation_version": "1", "workflow_only": True},
        )

    def execute(self, **params):
        try:
            params = PARAMETERS[self.tool_id].model_validate(params).model_dump()
            value = self.service.perform(
                self.owner, self.tool_id, self.input_value, params
            )
            return ToolResult(
                tool_name=self.tool_id,
                success=True,
                content=json.dumps(value),
                metadata={"workflow_output": value},
            )
        except Exception as exc:
            logger.exception("Workflow operation %s failed", self.tool_id)
            # Only curated value errors are public; provider/server errors stay private.
            detail = (
                str(exc)
                if isinstance(exc, (ValueError, ArtifactError))
                else (
                    "Operation failed; check the local service and administrator logs"
                )
            )
            return ToolResult(
                tool_name=self.tool_id, success=False, content=detail[:500]
            )


class WorkflowJobs:
    def __init__(
        self,
        artifacts: ArtifactStore,
        *,
        hunyuan=None,
        config=None,
        parent_policy=None,
        parent_agent="workflow",
        bus=None,
        boundary_guard=None,
    ):
        if config is not None and config.server.workers != 1:
            raise ValueError("Workflow jobs require one OpenJarvis API worker")
        self.artifacts, self.hunyuan, self.config = artifacts, hunyuan, config
        self.store = JobsStore(artifacts.root / "workflows" / "jobs.db")
        self.capabilities = create_builtin_capability_registry()
        self.parent_policy, self.parent_agent, self.bus = (
            parent_policy,
            parent_agent,
            bus,
        )
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.worker = None
        self.owner_check = None
        self.boundary_guard = boundary_guard

    def record(self, operation):
        registry = ToolManagementRegistry()
        record = sync_managed_tool(
            registry,
            OperationTool(self, operation),
            identity=f"builtin:{operation}",
            provenance=Provenance("workflow", operation, "1"),
            capability_registry=self.capabilities,
            implementation_id=f"{OperationTool.__module__}.OperationTool:{operation}:1",
        )
        with self.store.db() as db:
            approval = db.execute(
                "SELECT * FROM approvals WHERE operation=?", (operation,)
            ).fetchone()
        if (
            approval
            and approval["enabled"]
            and approval["fingerprint"] == record.fingerprint.value
        ):
            registry.approve(
                record.identity,
                ApprovalRecord(
                    approval_id=f"workflow:{operation}:{approval['fingerprint']}",
                    approved_by=approval["actor"],
                    timestamp=datetime.now(timezone.utc),
                    validation_id=record.validation.validation_id,
                    fingerprint=record.fingerprint,
                ),
            )
        return registry, record

    def approved(self, operation):
        if operation not in PARAMETERS:
            return False
        _, record = self.record(operation)
        return record.is_approved()

    def approve(self, actor, operation, enabled):
        if operation not in PARAMETERS:
            raise ValueError("Unknown operation")
        _, record = self.record(operation)
        with self.store.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO approvals VALUES(?,?,?,?)",
                (operation, record.fingerprint.value, actor, int(enabled)),
            )
            self.store.log(
                db,
                actor,
                "enable_operation" if enabled else "disable_operation",
                operation,
            )

    def operations(self):
        return [
            {
                "id": name,
                "description": DESCRIPTIONS[name],
                "parameters": model.model_json_schema(),
                "enabled": self.approved(name),
                "capabilities": OperationTool(self, name).spec.required_capabilities,
            }
            for name, model in PARAMETERS.items()
        ]

    def instances(self, admin=False):
        with self.store.db() as db:
            return [
                {
                    "id": r["id"],
                    "revision": r["revision"],
                    "enabled": bool(r["enabled"]),
                    **(
                        json.loads(r["config"])
                        if admin
                        else {"name": json.loads(r["config"])["name"]}
                    ),
                }
                for r in db.execute("SELECT * FROM instances ORDER BY rowid")
            ]

    def instance(self, identity):
        with self.store.db() as db:
            row = db.execute(
                "SELECT * FROM instances WHERE id=? AND enabled=1", (identity,)
            ).fetchone()
        if not row:
            raise ValueError(
                "Image server is missing or disabled; contact the administrator"
            )
        return ImageInstance.model_validate(json.loads(row["config"]))

    def save_instance(self, actor, values, identity=None, revision=None):
        values = ImageInstance.model_validate(values).model_dump()
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if identity:
                old = db.execute(
                    "SELECT revision FROM instances WHERE id=?", (identity,)
                ).fetchone()
                if not old or old[0] != revision:
                    raise ValueError("Image server changed; reload before saving")
                revision += 1
                db.execute(
                    "UPDATE instances SET revision=?,config=?,enabled=0 WHERE id=?",
                    (revision, json.dumps(values), identity),
                )
            else:
                if db.execute("SELECT count(*) FROM instances").fetchone()[0] >= 20:
                    raise ValueError("Image server limit reached")
                identity, revision = str(uuid.uuid4()), 1
                db.execute(
                    "INSERT INTO instances VALUES(?,?,?,0)",
                    (identity, revision, json.dumps(values)),
                )
            self.store.log(db, actor, "save_image_server", identity)
        return {"id": identity, "revision": revision, **values, "enabled": False}

    def submit(self, owner, workflow_id, revision, inputs):
        self.check_owner(owner)
        if self.stopping.is_set():
            raise ValueError("Workflow service is stopping")
        row = self.store.get_definition(owner, workflow_id)
        if not row:
            raise KeyError("Workflow not found")
        if row["revision"] != revision:
            raise ValueError("Workflow changed; reload before running")
        if set(inputs) - {"artifact_id", "prompt", "units"}:
            raise ValueError("Use an artifact ID and/or a text prompt")
        if inputs.get("artifact_id"):
            self.artifacts.read(owner, inputs["artifact_id"])
        if (
            not isinstance(inputs.get("prompt", ""), str)
            or len(inputs.get("prompt", "")) > 4000
        ):
            raise ValueError("Prompt must be text up to 4000 characters")
        if "units" in inputs:
            raise ValueError(
                "Units are established by a mesh scale step, not input labels"
            )
        definition = Definition.model_validate(row["definition"])
        for step in definition.steps:
            if not self.approved(step.operation):
                raise ValueError(
                    f"Operation {step.operation} needs administrator approval"
                )
        identity = str(uuid.uuid4())
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute(
                    "SELECT count(*) FROM runs WHERE state IN ('queued','running')"
                ).fetchone()[0]
                >= 16
            ):
                raise ValueError("Workflow queue is full; retry later")
            if (
                db.execute(
                    "SELECT count(*) FROM runs WHERE owner=?", (owner,)
                ).fetchone()[0]
                >= 200
            ):
                raise ValueError("Run history limit reached; delete completed runs")
            db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,0)",
                (
                    identity,
                    owner,
                    workflow_id,
                    revision,
                    json.dumps(row["definition"]),
                    json.dumps(inputs),
                    "queued",
                    json.dumps({"steps": []}),
                    time.time(),
                ),
            )
            self.store.log(db, owner, "submit_run", identity)
        self.start_worker()
        return self.store.run(owner, identity)

    def start_worker(self):
        with self.lock:
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(
                    target=self._work, daemon=True, name="openjarvis-workflows"
                )
                self.worker.start()
        self.wake.set()

    def subscribe(self, owner, job_id, workflow_id, revision):
        self.check_owner(owner)
        if self.hunyuan is None:
            raise ValueError("Hunyuan is not configured")
        self.hunyuan.own(owner, job_id)
        row = self.store.get_definition(owner, workflow_id)
        if not row or row["revision"] != revision:
            raise ValueError("Workflow missing or changed; reload")
        for step in Definition.model_validate(row["definition"]).steps:
            if not self.approved(step.operation):
                raise ValueError(
                    f"Operation {step.operation} needs administrator approval"
                )
        with self.store.db() as db:
            if (
                db.execute(
                    "SELECT count(*) FROM triggers WHERE owner=?", (owner,)
                ).fetchone()[0]
                >= 100
            ):
                raise ValueError("Automatic workflow limit reached")
            try:
                db.execute(
                    "INSERT INTO triggers VALUES(?,?,?,?,?,'waiting',NULL,NULL)",
                    (str(uuid.uuid4()), owner, job_id, workflow_id, revision),
                )
            except sqlite3.IntegrityError:
                raise ValueError(
                    "This 3D job already has an automatic workflow"
                ) from None
            self.store.log(db, owner, "subscribe_3d_workflow", job_id)
        self.start_worker()

    def triggers(self, owner):
        with self.store.db() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM triggers WHERE owner=? ORDER BY rowid DESC", (owner,)
                )
            ]

    def import_hunyuan(self, owner, job_id):
        if self.hunyuan is None:
            raise ValueError("Hunyuan is not configured")
        if self.hunyuan.status(owner, job_id).get("status") != "completed":
            raise ValueError("3D job has not completed")
        from openjarvis.workflow.mesh_worker import check_glb
        from openjarvis.workflow.providers import request

        data = request(self.hunyuan.url, "/jobs/" + job_id + "/download", binary=True)
        check_glb(data)
        return self.save_bytes(owner, job_id + ".glb", data)

    def poll_triggers(self):
        with self.store.db() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM triggers WHERE state='waiting' LIMIT 16"
                )
            ]
        for row in rows:
            if self.stopping.is_set():
                return
            try:
                state = self.hunyuan.status(row["owner"], row["job_id"])
                if state.get("status") in {"running", "queued"}:
                    continue
                if state.get("status") != "completed":
                    raise ValueError("3D generation did not complete")
                with self.store.db() as db:
                    if not db.execute(
                        "UPDATE triggers SET state='dispatching' "
                        "WHERE id=? AND state='waiting'",
                        (row["id"],),
                    ).rowcount:
                        continue
                artifact = self.import_hunyuan(row["owner"], row["job_id"])
                run = self.submit(
                    row["owner"],
                    row["workflow_id"],
                    row["revision"],
                    {"artifact_id": artifact["id"]},
                )
                with self.store.db() as db:
                    db.execute(
                        "UPDATE triggers SET state='submitted',run_id=? WHERE id=?",
                        (run["id"], row["id"]),
                    )
            except Exception:
                with self.store.db() as db:
                    db.execute(
                        "UPDATE triggers SET state='failed',error=? WHERE id=?",
                        (
                            "Automatic workflow failed; check the job, saved revision "
                            "and operation approvals",
                            row["id"],
                        ),
                    )

    def _work(self):
        while not self.stopping.is_set():
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT * FROM runs WHERE state='queued' ORDER BY created LIMIT 1"
                ).fetchone()
                if row:
                    db.execute(
                        "UPDATE runs SET state='running' WHERE id=?", (row["id"],)
                    )
            if not row:
                self.poll_triggers()
                self.wake.wait(5)
                self.wake.clear()
                continue
            self._execute(dict(row))

    def _execute(self, row):
        steps = []
        values = {"input": json.loads(row["input"])}
        identity = ExecutionIdentity(user_id=row["owner"], trace_id=row["id"])
        try:
            with execution_scope(identity):
                for step in Definition.model_validate(
                    json.loads(row["snapshot"])
                ).steps:
                    if self.cancelled(row["owner"], row["id"]):
                        self.store.update(row["id"], "cancelled", {"steps": steps})
                        return
                    item = {
                        "id": step.id,
                        "operation": step.operation,
                        "status": "running",
                        "parameters": step.parameters,
                        "input": values[step.input],
                    }
                    steps.append(item)
                    self.store.update(row["id"], "running", {"steps": steps})
                    registry, _ = self.record(step.operation)
                    tool = OperationTool(
                        self, step.operation, row["owner"], values[step.input]
                    )
                    executor = ToolExecutor(
                        [tool],
                        self.bus,
                        capability_policy=OperationPolicy(self),
                        agent_id=f"workflow:{row['owner']}",
                        tool_management_registry=registry,
                        boundary_guard=self.boundary_guard,
                    )
                    started = time.monotonic()
                    result = executor.execute(
                        ToolCall(
                            id=f"{row['id']}:{step.id}",
                            name=step.operation,
                            arguments=json.dumps(step.parameters),
                        )
                    )
                    item.update(
                        status="completed" if result.success else "failed",
                        elapsed_seconds=round(time.monotonic() - started, 3),
                    )
                    if not result.success:
                        item["error"] = result.content[:500]
                        self.store.update(
                            row["id"],
                            "failed",
                            {"steps": steps, "error": item["error"]},
                        )
                        return
                    value = result.metadata["workflow_output"]
                    item["output"] = value
                    values[step.id] = value
                    self.store.update(row["id"], "running", {"steps": steps})
                state = (
                    "cancelled"
                    if self.cancelled(row["owner"], row["id"])
                    else "completed"
                )
                self.store.update(row["id"], state, {"steps": steps, "output": value})
        except Exception:
            logger.exception("Workflow run %s failed", row["id"])
            self.store.update(
                row["id"],
                "failed",
                {
                    "steps": steps,
                    "error": "Workflow failed; administrator can inspect server logs",
                },
            )

    def cancelled(self, owner, identity):
        return (
            self.stopping.is_set()
            or self.store.run(owner, identity)["cancel_requested"]
        )

    def close(self):
        self.stopping.set()
        self.wake.set()
        if self.worker:
            self.worker.join(timeout=2)

    def save_bytes(self, owner, filename, data):
        self.check_owner(owner)
        return self.artifacts.save(
            owner, filename, base64.b64encode(data).decode(), "base64"
        )

    def perform(self, owner, operation, value, params):
        self.check_owner(owner)
        if operation == "image_generate_local":
            from openjarvis.workflow.providers import generate_image

            instance = self.instance(params["instance_id"])
            data = generate_image(instance, value.get("prompt", ""), params)
            artifact = self.save_bytes(owner, "reference.png", data)
            return {
                "artifact_id": artifact["id"],
                "artifact": artifact,
                "prompt": value.get("prompt", ""),
                "provider": params["instance_id"],
                "checkpoint": instance.checkpoint,
            }
        if operation == "hunyuan_generate":
            from openjarvis.workflow.providers import generate_mesh

            _, data = self.artifacts.read(owner, value.get("artifact_id"))
            if self.hunyuan is None:
                raise ValueError("Local Hunyuan generation is not configured")
            job_id, glb = generate_mesh(self.hunyuan, owner, data, params, self.config)
            artifact = self.save_bytes(owner, "generated.glb", glb)
            return {
                "artifact_id": artifact["id"],
                "artifact": artifact,
                "hunyuan_job_id": job_id,
            }
        row, data = self.artifacts.read(owner, value.get("artifact_id"))
        if operation == "artifact_copy":
            artifact = self.save_bytes(owner, row["filename"], data)
            return {**value, "artifact_id": artifact["id"], "artifact": artifact}
        if operation == "mesh_export_stl" and value.get("units") != "mm":
            raise ValueError(
                "Add a mesh_scale step with a target dimension before STL export"
            )
        from openjarvis.workflow.providers import mesh_operation

        output, report = mesh_operation(row["filename"], data, operation, params)
        self.check_owner(owner)
        report.update(
            input_units=value.get("units", "source"),
            output_units="mm"
            if operation == "mesh_scale"
            else value.get("units", "source"),
        )
        report_artifact = self.artifacts.save(
            owner,
            f"{operation}-report.json",
            json.dumps(
                {
                    **report,
                    "input_artifact": row,
                    "input_units": value.get("units", "source"),
                    "output_units": "mm"
                    if operation == "mesh_scale"
                    else value.get("units", "source"),
                },
                indent=2,
            ),
        )
        result = {**value, "report": report, "report_artifact": report_artifact}
        if output is not None:
            artifact = self.save_bytes(
                owner,
                "processed.stl" if operation == "mesh_export_stl" else "processed.ply",
                output,
            )
            result.update(artifact_id=artifact["id"], artifact=artifact)
        if operation == "mesh_scale":
            result["units"] = "mm"
        return result

    def check_owner(self, owner):
        if self.stopping.is_set():
            raise ValueError("Workflow service is stopping")
        if self.owner_check is not None and not self.owner_check(owner):
            raise ValueError("Workflow account is missing or disabled")
