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


class APITemplateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    definition: dict
    revision: StrictInt = Field(default=0, ge=0)


class APIImportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=65536)


class SharingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    sharing: str = Field(pattern="^(personal|pending|shared)$")


class SourcePreference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


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
            except SourceConflict as exc:
                raise HTTPException(409, str(exc)) from None
            except (ValueError, OSError) as exc:
                raise HTTPException(400, str(exc)) from None
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
    from openjarvis.connectors.source_access import SourceAccess
    from openjarvis.server.auth import get_auth_store

    def access(request):
        user = None
        token = request.headers.get("X-OpenJarvis-Session", "").strip()
        if token:
            user = get_auth_store(request).get_user_for_token(token)
            if user is None:
                raise HTTPException(401, "Invalid session")
        if user is not None:
            request.state.auth_user_id = str(user["user_id"])
            return SourceAccess(
                manager.store, str(user["user_id"]), bool(user["is_admin"])
            )
        if getattr(request.state, "server_admin_access", False):
            return SourceAccess(manager.store, admin=True)
        raise HTTPException(401, "Sign in to manage sources")

    class AuthorizedSourceRoute(SafeSourceRoute):
        def get_route_handler(self):
            original = super().get_route_handler()

            async def handler(request):
                # External OAuth tickets have their own one-use caller binding.
                if self.path.endswith(("/oauth/launch", "/oauth/callback")):
                    return await original(request)
                policy = access(request)
                request.state.source_access = policy
                source_id = next(
                    (
                        request.path_params[k]
                        for k in ("source_id", "identity", "connector_id")
                        if k in request.path_params
                    ),
                    None,
                )
                if source_id:
                    import uuid

                    try:
                        uuid.UUID(source_id)
                    except ValueError:
                        raise HTTPException(400, "Invalid source identity") from None
                    if policy.admin and request.url.path.endswith("/audit"):
                        return await original(request)
                    try:
                        record = manager.store.get(source_id)
                    except KeyError:
                        raise HTTPException(404, "Source not found") from None
                    if not policy.visible(record):
                        raise HTTPException(404, "Source not found")
                    preference = request.url.path.endswith("/preference")
                    sharing = request.url.path.endswith("/sharing")
                    if (
                        not preference
                        and not policy.manages(record)
                        and not (sharing and policy.admin)
                    ):
                        raise HTTPException(
                            403,
                            "Only the source owner or administrator may manage this "
                            "source",
                        )
                elif "/imports" in request.url.path or request.url.path.endswith(
                    "/audit"
                ):
                    if not policy.admin:
                        raise HTTPException(403, "Administrator access required")
                credential_id = request.path_params.get("credential_id")
                if credential_id and not policy.credential_allowed(
                    manager.credentials, credential_id
                ):
                    raise HTTPException(404, "Credential not found")
                if credential_id and request.method != "GET":
                    shared = [
                        r
                        for r in manager.store.list()
                        if r["sharing"] == "shared"
                        and r["config"].get("credential_id") == credential_id
                    ]
                    if shared and not policy.admin:
                        raise HTTPException(
                            403,
                            "An approved universal source uses this credential; "
                            "administrator access required",
                        )
                    for record in shared:
                        with manager._locked(record["id"]):
                            invoke(
                                manager.store.set_sharing,
                                record["id"],
                                record["revision"],
                                "pending",
                                actor=actor(request),
                            )
                if (
                    request.method in {"POST", "PUT"}
                    and request.url.path in {"/v1/sources", "/v1/sources/test"}
                    or request.method == "PUT"
                    and source_id
                    and request.url.path == f"/v1/sources/{source_id}"
                ):
                    try:
                        body = await request.json()
                    except ValueError:
                        raise HTTPException(422, "Invalid source request") from None
                    config = body.get("config", {}) if isinstance(body, dict) else {}
                    credential_id = (
                        config.get("credential_id")
                        if isinstance(config, dict)
                        else None
                    )
                    if credential_id and not policy.credential_allowed(
                        manager.credentials, credential_id
                    ):
                        raise HTTPException(404, "Credential not found")
                return await original(request)

            return handler

    router = APIRouter(
        prefix="/v1/sources", tags=["sources"], route_class=AuthorizedSourceRoute
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

    from openjarvis.server.source_connections import install_source_connections

    install_source_connections(router, manager, invoke)

    from openjarvis.connectors.source_imports import SourceImports

    imports = SourceImports(manager)

    from openjarvis.connectors.api_templates import APITemplates, import_definition

    templates = APITemplates(manager.store)

    @router.get("/api-templates")
    def api_templates(request: Request):
        return {"templates": templates.list(request.state.source_access.user_id)}

    @router.post("/api-templates", status_code=201)
    def create_api_template(req: APITemplateInput, request: Request):
        return templates.save(
            request.state.source_access.user_id, req.name, req.definition
        )

    @router.put("/api-templates/{template_id}")
    def update_api_template(template_id: str, req: APITemplateInput, request: Request):
        return templates.save(
            request.state.source_access.user_id,
            req.name,
            req.definition,
            template_id,
            req.revision,
        )

    @router.delete("/api-templates/{template_id}", status_code=204)
    def delete_api_template(template_id: str, revision: int, request: Request):
        templates.remove(request.state.source_access.user_id, template_id, revision)

    @router.post("/api-import")
    def import_api(req: APIImportInput):
        return {
            "definition": import_definition(req.text),
            "notice": (
                "Draft only. Review authentication, bodies, response "
                "mapping and operation kind before saving."
            ),
        }

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
    def credentials(request: Request):
        policy = request.state.source_access
        return {
            "credentials": [
                row
                for row in invoke(manager.credentials.list)
                if policy.credential_allowed(manager.credentials, row["id"])
            ]
        }

    @router.post("/credentials", status_code=201)
    def create_credential(req: CredentialInput, request: Request):
        return invoke(
            manager.credentials.create,
            req.name,
            req.kind,
            req.origin,
            req.secret.get_secret_value(),
            req.header_name,
            owner_id=request.state.source_access.user_id,
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
    def sources(request: Request):
        policy = request.state.source_access
        records = []
        for record in manager.list():
            if not policy.visible(record):
                continue
            record["can_manage"] = policy.manages(record)
            record["use_enabled"] = manager.store.preference(
                policy.user_id, record["id"]
            )
            if not record["can_manage"]:
                record = {
                    key: record[key]
                    for key in (
                        "id",
                        "adapter_id",
                        "name",
                        "enabled",
                        "sharing",
                        "revision",
                        "owner_id",
                        "can_manage",
                        "use_enabled",
                        "config_version",
                        "state",
                        "configuration_state",
                    )
                    if key in record
                }
                record.update(config={}, error=None)
            records.append(record)
        return {"sources": records, "can_approve": policy.admin}

    @router.put("/{source_id}/sharing")
    def sharing(source_id: str, req: SharingInput, request: Request):
        policy = request.state.source_access
        if req.sharing == "shared" and not policy.admin:
            raise HTTPException(403, "Administrator approval required")
        with manager._locked(source_id):
            return invoke(
                manager.store.set_sharing,
                source_id,
                req.revision,
                req.sharing,
                actor=actor(request),
            )

    @router.put("/{source_id}/preference")
    def preference(source_id: str, req: SourcePreference, request: Request):
        policy = request.state.source_access
        if not policy.user_id:
            raise HTTPException(401, "Human login required")
        invoke(manager.store.set_preference, policy.user_id, source_id, req.enabled)
        return {"enabled": req.enabled}

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
