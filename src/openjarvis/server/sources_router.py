"""Authenticated web management of named, database-backed source instances."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, StrictInt

from openjarvis.connectors.source_adapters import list_adapters
from openjarvis.connectors.source_audit import list_events
from openjarvis.connectors.source_manager import SourceManager
from openjarvis.connectors.source_store import SourceConflict


class SourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapter_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=120)
    config: dict


class SourceEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=120)
    config: dict
    enabled: StrictBool


class SourceTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapter_id: str
    config: dict


class SafeSourceRoute(APIRoute):
    """Validation errors must not echo credential input values."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError:
                raise HTTPException(
                    422, "Invalid source or credential request"
                ) from None

        return handler


class LegacyImportPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)


class LegacyImportApply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_token: str = Field(min_length=1, max_length=4096)


class CredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    kind: str
    origin: str
    secret: SecretStr
    header_name: str = ""


class CredentialRotation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    secret: SecretStr


class SourceScheduleEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=0)
    enabled: StrictBool
    interval_seconds: StrictInt = Field(ge=300, le=604800)


class MigrationPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=1)


class MigrationApply(MigrationPreview):
    plan_token: str = Field(pattern=r"^[a-f0-9]{64}$")


def create_sources_router(manager: SourceManager | None = None) -> APIRouter:
    manager = manager or SourceManager()
    router = APIRouter(
        prefix="/v1/sources", tags=["sources"], route_class=SafeSourceRoute
    )

    @router.on_event("startup")
    def start_jobs():
        manager.start_jobs()

    @router.on_event("shutdown")
    def stop_jobs():
        manager.stop_jobs()

    def invoke(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except KeyError as exc:
            raise HTTPException(404, "Source not found") from exc
        except SourceConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    def actor(request):
        user_id = getattr(request.state, "auth_user_id", None)
        return f"user:{user_id}" if user_id is not None else "server_access"

    from openjarvis.connectors.source_imports import SourceImports

    imports = SourceImports(manager)

    @router.get("/imports")
    def legacy_imports():
        return {"imports": invoke(imports.list)}

    @router.post("/imports/{import_id}/preview")
    def preview_legacy_import(
        import_id: str, req: LegacyImportPreview, request: Request
    ):
        return invoke(imports.preview, import_id, req.name, actor=actor(request))

    @router.post("/imports/{import_id}")
    def apply_legacy_import(import_id: str, req: LegacyImportApply, request: Request):
        result = invoke(imports.apply, import_id, req.plan_token, actor=actor(request))
        result.pop("legacy_document_ids")
        return result

    @router.get("/audit")
    def audit(before_id: int | None = Query(None, ge=1)):
        return {"events": invoke(list_events, manager.store, before_id=before_id)}

    @router.get("/credentials")
    def credentials():
        return {"credentials": invoke(manager.credentials.list)}

    @router.post("/credentials", status_code=201)
    def create_credential(req: CredentialInput):
        return invoke(
            manager.credentials.create,
            req.name,
            req.kind,
            req.origin,
            req.secret.get_secret_value(),
            req.header_name,
        )

    @router.put("/credentials/{credential_id}")
    def rotate_credential(credential_id: str, req: CredentialRotation):
        return invoke(
            manager.credentials.rotate,
            credential_id,
            req.revision,
            req.secret.get_secret_value(),
        )

    @router.delete("/credentials/{credential_id}", status_code=204)
    def remove_credential(credential_id: str, revision: int):
        invoke(manager.delete_credential, credential_id, revision)

    @router.get("/adapters")
    def adapters():
        return {"adapters": list_adapters()}

    @router.post("/test")
    def test_source(req: SourceTest):
        return invoke(manager.test, req.adapter_id, req.config)

    @router.get("")
    def sources():
        return {"sources": manager.list()}

    @router.post("", status_code=201)
    def create_source(req: SourceInput, request: Request):
        result = invoke(
            manager.create, req.adapter_id, req.name, req.config, actor=actor(request)
        )
        result.pop("legacy_document_ids")
        return result

    @router.put("/{source_id}")
    def edit_source(source_id: str, req: SourceEdit, request: Request):
        result = invoke(
            manager.update,
            source_id,
            req.revision,
            name=req.name,
            config=req.config,
            enabled=req.enabled,
            actor=actor(request),
        )
        result.pop("legacy_document_ids")
        return result

    @router.delete("/{source_id}", status_code=204)
    def remove_source(source_id: str, revision: int, request: Request):
        invoke(manager.delete, source_id, revision, actor=actor(request))

    @router.get("/{source_id}/audit")
    def source_audit(source_id: str, before_id: int | None = Query(None, ge=1)):
        return {
            "events": invoke(list_events, manager.store, source_id, before_id=before_id)
        }

    @router.post("/{source_id}/migration/preview")
    def preview_migration(source_id: str, req: MigrationPreview):
        return invoke(manager.migration_preview, source_id, req.revision)

    @router.post("/{source_id}/migration")
    def apply_migration(source_id: str, req: MigrationApply, request: Request):
        return invoke(
            manager.migrate,
            source_id,
            req.revision,
            req.plan_token,
            actor=actor(request),
        )

    @router.get("/{source_id}/jobs")
    def job_history(source_id: str):
        return {"jobs": invoke(manager.jobs.history, source_id)}

    @router.put("/{source_id}/schedule")
    def set_schedule(source_id: str, req: SourceScheduleEdit, request: Request):
        return invoke(
            manager.jobs.set_schedule,
            source_id,
            req.revision,
            enabled=req.enabled,
            interval_seconds=req.interval_seconds,
            actor=actor(request),
        )

    @router.post("/{source_id}/cancel", status_code=202)
    def cancel_sync(source_id: str):
        return invoke(manager.cancel_sync, source_id)

    @router.post("/{source_id}/sync", status_code=202)
    def sync_source(source_id: str):
        job = invoke(manager.start_sync, source_id)
        return {"source_id": source_id, "status": "started", "job": job}

    return router
