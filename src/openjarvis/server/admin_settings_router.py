"""Administrator parameter editor and live model-default updates."""

import copy
import threading

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from openjarvis.core.admin_settings import (
    LIVE,
    MODEL_FIELDS,
    AdminSettingsStore,
    SettingsConflict,
    fields,
    set_value,
    settings_path,
)
from openjarvis.server.auth import authenticate_admin_request


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=1)
    changes: dict = Field(max_length=500)


class SettingsRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe(request):
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(
                    422,
                    "Submit a current integer revision and named setting changes. "
                    "Reload the settings screen and retry.",
                ) from None

        return safe


def create_admin_settings_router(app, config):
    store = AdminSettingsStore(settings_path(config))
    baseline = copy.deepcopy(config)
    # Bootstrap values are needed for Reset, rather than the already-overlaid values.
    source = getattr(config, "_settings_bootstrap", None)
    if source is not None:
        baseline = source
    schema = fields(baseline)
    bootstrap_model = (
        baseline.server.model or baseline.intelligence.default_model or app.state.model
    )
    lock = threading.Lock()
    app.state.admin_settings_store = store
    router = APIRouter(
        prefix="/v1/admin-settings",
        tags=["administrator settings"],
        dependencies=[Depends(authenticate_admin_request)],
        route_class=SettingsRoute,
    )

    def listing():
        revision, overrides = store.read()
        active_fields = fields(config)
        result = copy.deepcopy(list(schema.values()))
        for field in result:
            if field["editable"]:
                key = field["key"]
                field["value"] = overrides.get(key, field["value"])
                field["overridden"] = key in overrides
                field["bootstrap_value"] = schema[key]["value"]
                active = active_fields[key]["value"]
                field["active_value"] = active
                field["pending_restart"] = (
                    field["application"] == "restart" and field["value"] != active
                )
        return {
            "revision": revision,
            "fields": result,
            "active_default_model": app.state.model,
        }

    @router.get("")
    def get(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return listing()

    @router.put("")
    def update(
        body: Edit, request: Request, actor: str = Depends(authenticate_admin_request)
    ):
        changes = dict(body.changes)
        # One default, with both legacy names persisted for restart compatibility.
        keys = {"server.model", "intelligence.default_model"} & changes.keys()
        if keys:
            values = [changes[k] for k in keys]
            if len(values) == 2 and values[0] != values[1]:
                raise HTTPException(400, "Server and intelligence defaults must match")
            for key in ("server.model", "intelligence.default_model"):
                changes[key] = values[0]
        with lock:
            try:
                from openjarvis.core.admin_settings import validate

                validate(schema, changes)
                for key in MODEL_FIELDS & changes.keys():
                    value = changes[key]
                    if value is None or value == "":
                        if value == "" and key != "tools.storage.extraction_model":
                            raise ValueError(
                                "Choose an installed model or Reset to bootstrap"
                            )
                        continue
                    available = app.state.engine.list_models()
                    from openjarvis.server.model_capabilities import is_embed_only_model

                    if value not in available or is_embed_only_model(value):
                        raise ValueError(
                            "Choose a chat model installed on the existing backend. "
                            "Configured-server models use task assignments."
                        )
                authenticate_admin_request(request)
                _, overrides = store.update(body.revision, changes, actor, schema)
            except SettingsConflict as exc:
                raise HTTPException(409, str(exc)) from None
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            except TypeError:
                raise HTTPException(
                    400,
                    "Invalid setting value. Use the displayed type "
                    "and an installed backend model.",
                ) from None
            for key in LIVE:
                if key in changes:
                    set_value(config, key, overrides.get(key, schema[key]["value"]))
            if keys:
                app.state.model = (
                    config.server.model
                    or config.intelligence.default_model
                    or bootstrap_model
                )
                agent = getattr(app.state, "agent", None)
                if agent is not None and hasattr(agent, "_model"):
                    from openjarvis.server.routes import _get_agent_model_lock

                    with _get_agent_model_lock(agent):
                        agent._model = app.state.model
            svc = getattr(app.state, "memory_service", None)
            if svc is not None and (
                keys or "tools.storage.extraction_model" in changes
            ):
                svc._extractor.configure_model(
                    app.state.engine, config.memory.extraction_model or app.state.model
                )
        return listing()

    return router
