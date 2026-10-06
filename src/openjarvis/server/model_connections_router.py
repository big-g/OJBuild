"""Live administrator-only model-server configuration and discovery."""

import sqlite3

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from openjarvis.engine.connection_discovery import discover, inspect_model
from openjarvis.engine.connection_store import ConnectionConflict, adapter_definition
from openjarvis.server.auth import authenticate_admin_request


class Connection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=24)
    url: str = Field(min_length=1, max_length=2048)


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=1)


class Edit(Connection, Revision):
    pass


class ModelRevision(Revision):
    serving_id: str = Field(min_length=1, max_length=256)


class Enabled(Revision):
    enabled: StrictBool


class SafeRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe(request):
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(
                    422,
                    "Expected a 1–24 character name, root URL "
                    "and current integer revision for edits/tests.",
                ) from None

        return safe


def operation(fn):
    try:
        return fn()
    except KeyError:
        raise HTTPException(404, "Model connection not found") from None
    except ConnectionConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Connection name already exists") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


def create_model_connections_router(store):
    router = APIRouter(
        prefix="/v1/model-connections",
        tags=["model connections"],
        dependencies=[Depends(authenticate_admin_request)],
        route_class=SafeRoute,
    )

    @router.get("/adapters")
    def adapters():
        return {"adapters": [adapter_definition()]}

    @router.get("")
    def listing(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {"connections": store.list()}

    @router.post("", status_code=201)
    def create(body: Connection, actor: str = Depends(authenticate_admin_request)):
        return operation(lambda: store.create(body.name, body.url, actor))

    @router.put("/{identity}")
    def edit(
        identity: str, body: Edit, actor: str = Depends(authenticate_admin_request)
    ):
        return operation(
            lambda: store.update(identity, body.revision, body.name, body.url, actor)
        )

    @router.delete("/{identity}", status_code=204)
    def remove(
        identity: str,
        revision: int = Query(ge=1),
        actor: str = Depends(authenticate_admin_request),
    ):
        operation(lambda: store.remove(identity, revision, actor))
        return Response(status_code=204)

    @router.get("/{identity}/audit")
    def audit(identity: str):
        return {"events": store.audit(identity)}

    @router.post("/{identity}/enabled")
    def enabled(
        identity: str, body: Enabled, actor: str = Depends(authenticate_admin_request)
    ):
        return operation(
            lambda: store.enable(identity, body.revision, body.enabled, actor)
        )

    @router.post("/{identity}/capabilities")
    def capabilities(
        identity: str,
        body: ModelRevision,
        request: Request,
        actor: str = Depends(authenticate_admin_request),
    ):
        connection = operation(lambda: store.get(identity, body.revision))
        if connection["adapter_id"] != "ollama" or connection["config_version"] != 1:
            raise HTTPException(400, "Unsupported model adapter/settings version")
        if not any(m["serving_id"] == body.serving_id for m in connection["catalog"]):
            raise HTTPException(400, "Model is not in this connection's catalog")
        try:
            caps = inspect_model(connection, body.serving_id)
        except (httpx.HTTPError, OSError, ValueError, RecursionError):
            authenticate_admin_request(request)
            updated = operation(
                lambda: store.model_capabilities(
                    identity, body.revision, body.serving_id, None, actor
                )
            )
            return {
                "ok": False,
                "connection": updated,
                "message": "Capability read failed. Check Ollama availability/version, "
                "then test the catalog and read capabilities again.",
            }
        authenticate_admin_request(request)
        updated = operation(
            lambda: store.model_capabilities(
                identity, body.revision, body.serving_id, caps, actor
            )
        )
        return {
            "ok": True,
            "connection": updated,
            "message": "Ollama capabilities recorded. Enable this connection for chat "
            "when ready. Provider reports are not behavioral verification.",
        }

    @router.post("/{identity}/test")
    def test(
        identity: str,
        body: Revision,
        request: Request,
        actor: str = Depends(authenticate_admin_request),
    ):
        connection = operation(lambda: store.get(identity, body.revision))
        if connection["adapter_id"] != "ollama" or connection["config_version"] != 1:
            raise HTTPException(
                400,
                "Unsupported model adapter or settings version. "
                "Upgrade the server before testing this connection.",
            )
        try:
            catalog = discover(connection)
        except (httpx.HTTPError, OSError, ValueError, RecursionError):
            authenticate_admin_request(request)
            updated = operation(
                lambda: store.discovered(identity, body.revision, [], False, actor)
            )
            return {
                "ok": False,
                "connection": updated,
                "message": "Catalog test failed. Check the Ollama service, IP, port, "
                "TLS certificate and firewall. Redirects are unsupported.",
            }
        authenticate_admin_request(request)
        updated = operation(
            lambda: store.discovered(identity, body.revision, catalog, True, actor)
        )
        return {
            "ok": True,
            "connection": updated,
            "message": "Catalog discovered. Model capability and inference tests "
            "remain required.",
        }

    return router
