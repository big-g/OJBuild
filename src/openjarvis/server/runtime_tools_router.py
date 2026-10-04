"""Administrator-only runtime tool installation and review."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from openjarvis.server.auth import authenticate_request
from openjarvis.tools.runtime_adapters import list_adapters
from openjarvis.tools.runtime_store import TRANSFORMS, RuntimeToolConflict


class Definition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^custom_[a-z][a-z0-9_]{0,55}$")
    description: str = Field(min_length=1, max_length=500)
    transform: str | None = Field(default=None, max_length=32)
    adapter_id: str | None = Field(default=None, max_length=64)
    config: dict | None = None


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=1)


class Edit(Revision):
    definition: Definition


class Enabled(Revision):
    enabled: StrictBool


def create_runtime_tools_router(manager):
    def admin(request: Request):
        authenticate_request(request)
        if not request.state.auth_user["is_admin"]:
            raise HTTPException(403, "Administrator account required")
        return f"user:{request.state.auth_user_id}"

    router = APIRouter(
        prefix="/v1/runtime-tools",
        tags=["Runtime tools"],
        dependencies=[Depends(admin)],
    )

    def change(identity, body, actor, event, definition=None):
        try:
            row = manager.change(identity, body.revision, event, actor, definition)
            return manager.view(row) if row else {"deleted": True}
        except KeyError:
            raise HTTPException(404, "Runtime tool not found") from None
        except RuntimeToolConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.get("")
    def list_tools():
        return {
            "tools": [manager.view(row) for row in manager.store.list()],
            "transforms": TRANSFORMS,
            "adapters": list_adapters(),
        }

    @router.post("", status_code=201)
    def install(body: Definition, actor: str = Depends(admin)):
        try:
            return manager.view(
                manager.store.create(body.model_dump(exclude_none=True), actor)
            )
        except RuntimeToolConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.put("/{identity}")
    def edit(identity: str, body: Edit, actor: str = Depends(admin)):
        return change(
            identity,
            body,
            actor,
            "updated",
            body.definition.model_dump(exclude_none=True),
        )

    @router.post("/{identity}/approve")
    def approve(identity: str, body: Revision, actor: str = Depends(admin)):
        return change(identity, body, actor, "approved")

    @router.put("/{identity}/enabled")
    def enabled(identity: str, body: Enabled, actor: str = Depends(admin)):
        return change(identity, body, actor, "enabled" if body.enabled else "disabled")

    @router.delete("/{identity}")
    def remove(identity: str, body: Revision, actor: str = Depends(admin)):
        return change(identity, body, actor, "deleted")

    @router.get("/{identity}/audit")
    def audit(identity: str):
        # Audit history remains available after removal.
        import uuid

        try:
            uuid.UUID(identity)
        except ValueError:
            raise HTTPException(400, "Invalid runtime tool identity") from None
        return {"events": manager.store.audit(identity)}

    return router
