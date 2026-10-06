"""Administrator configuration never implies remote tool approval."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, StrictInt

from openjarvis.server.auth import authenticate_request
from openjarvis.tools.runtime_store import RuntimeToolConflict


class Connection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,23}$")
    url: str = Field(min_length=1, max_length=4096)
    bearer_token: SecretStr | None = None
    credential_secret: SecretStr | None = None
    auth_type: Literal["bearer", "api_key"] = "bearer"
    api_key_header: str = Field(default="", max_length=64)
    allow_without_confirmation: StrictBool = False
    network_access: Literal["public", "lan"] = "public"
    lan_addresses: str = Field(default="", max_length=2048)
    tls_trust: Literal["system", "custom_ca"] = "system"
    ca_certificate: str = Field(default="", max_length=16384)


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=1)


class LegacyImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: StrictInt = Field(ge=0, lt=128)
    review_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class Edit(Revision):
    definition: Connection


class Enabled(Revision):
    enabled: StrictBool


class SecretSafeRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # Return fixed guidance only; validation inputs can contain secrets.
                if any(
                    error["type"] == "string_pattern_mismatch"
                    and tuple(error["loc"])
                    in {("body", "name"), ("body", "definition", "name")}
                    for error in exc.errors()
                ):
                    raise HTTPException(
                        422,
                        "Connection name must be 1–24 characters: start with a "
                        "lowercase letter and use only lowercase letters, digits "
                        "or underscores. No spaces or hyphens. Example: home_tools.",
                    ) from None
                raise HTTPException(422, "Invalid MCP connection request") from None

        return safe


def create_runtime_mcp_router(manager):
    from openjarvis.mcp.legacy_import import LegacyMCPImporter

    importer = LegacyMCPImporter(manager)

    def legacy_config(request):
        config = getattr(request.app.state, "config", None)
        if config is None:
            from openjarvis.core.config import load_config

            config = load_config()
        return getattr(
            getattr(getattr(config, "tools", None), "mcp", None), "servers", ""
        )

    def admin(request: Request):
        authenticate_request(request)
        if not request.state.auth_user["is_admin"]:
            raise HTTPException(403, "Administrator account required")
        return f"user:{request.state.auth_user_id}"

    router = APIRouter(
        prefix="/v1/runtime-mcp",
        tags=["Runtime MCP"],
        dependencies=[Depends(admin)],
        route_class=SecretSafeRoute,
    )

    def config(body):
        return {
            "name": body.name,
            "url": body.url,
            "auth_type": body.auth_type,
            "api_key_header": body.api_key_header,
            "network_access": body.network_access,
            "lan_addresses": body.lan_addresses,
            "tls_trust": body.tls_trust,
            "ca_certificate": body.ca_certificate,
            "allow_without_confirmation": body.allow_without_confirmation,
        }

    def token(body):
        if body.credential_secret is not None:
            if body.bearer_token is not None:
                raise ValueError("Supply only one MCP credential field")
            return body.credential_secret.get_secret_value()
        if body.auth_type == "api_key" and body.bearer_token is not None:
            raise ValueError("Use credential_secret for API key authentication")
        return (
            body.bearer_token.get_secret_value()
            if body.bearer_token is not None
            else None
        )

    def operation(fn):
        try:
            row = fn()
            return manager.view(row) if row else {"deleted": True}
        except KeyError:
            raise HTTPException(404, "MCP connection not found") from None
        except RuntimeToolConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.get("")
    def list_connections():
        return {"connections": [manager.view(row) for row in manager.store.list()]}

    @router.post("", status_code=201)
    def create(body: Connection, actor: str = Depends(admin)):
        return operation(
            lambda: manager.store.create(config(body), actor, token(body) or "")
        )

    @router.get("/imports/legacy")
    def review_legacy(request: Request):
        try:
            return {"entries": importer.review(legacy_config(request))}
        except ValueError:
            raise HTTPException(
                400, "Legacy MCP configuration is invalid or exceeds review limits"
            ) from None

    @router.post("/imports/legacy", status_code=201)
    def import_legacy(
        request: Request, body: LegacyImport, actor: str = Depends(admin)
    ):
        return operation(
            lambda: importer.import_one(
                legacy_config(request), body.index, body.review_digest, actor
            )
        )

    @router.put("/{identity}")
    def edit(identity: str, body: Edit, actor: str = Depends(admin)):
        return operation(
            lambda: manager.change(
                identity,
                body.revision,
                "updated",
                actor,
                config=config(body.definition),
                token=token(body.definition),
            )
        )

    @router.post("/{identity}/discover")
    def discover(identity: str, body: Revision, actor: str = Depends(admin)):
        return operation(lambda: manager.discover(identity, body.revision, actor))

    @router.post("/{identity}/approve")
    def approve(identity: str, body: Revision, actor: str = Depends(admin)):
        return operation(
            lambda: manager.change(identity, body.revision, "approved", actor)
        )

    @router.put("/{identity}/enabled")
    def enabled(identity: str, body: Enabled, actor: str = Depends(admin)):
        return operation(
            lambda: manager.change(
                identity,
                body.revision,
                "enabled" if body.enabled else "disabled",
                actor,
            )
        )

    @router.delete("/{identity}")
    def remove(identity: str, body: Revision, actor: str = Depends(admin)):
        return operation(
            lambda: manager.change(identity, body.revision, "deleted", actor)
        )

    @router.get("/{identity}/audit")
    def audit(identity: str):
        return {"events": manager.store.audit(identity)}

    return router
