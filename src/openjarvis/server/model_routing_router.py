"""Administrator task assignment and isolated diagnostic measurement endpoints."""

import threading
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from openjarvis.engine.model_benchmarks import run_diagnostic
from openjarvis.engine.task_routing import SUITE_VERSION, TaskRoutingStore
from openjarvis.server.auth import authenticate_admin_request
from openjarvis.server.model_connections_router import operation

Task = Literal["general", "coding", "analysis", "vision"]


class RoutingRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe(request):
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(
                    422,
                    "Use a known task, model ID, current integer "
                    "revision, boolean enabled and benchmark ID. "
                    "Reload the configuration screen and retry.",
                ) from None

        return safe


class Assignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=1)
    enabled: StrictBool
    model_id: str = Field(max_length=2048)
    benchmark_id: str = Field(max_length=32)
    fallback_model_id: str = Field(default="", max_length=2048)
    fallback_benchmark_id: str = Field(default="", max_length=32)


class Diagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: Task
    model_id: str = Field(min_length=1, max_length=2048)
    connection_revision: StrictInt = Field(ge=1)


def create_model_routing_router(connections):
    store = TaskRoutingStore(connections)
    busy = threading.Lock()
    router = APIRouter(
        prefix="/v1/model-routing",
        tags=["model routing"],
        dependencies=[Depends(authenticate_admin_request)],
        route_class=RoutingRoute,
    )

    @router.get("")
    def listing(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {
            "rules": store.rules(),
            "benchmarks": store.benchmarks(),
            "audit": store.audit(),
            "suite_version": SUITE_VERSION,
        }

    @router.put("/tasks/{task}")
    def assign(
        task: Task, body: Assignment, actor: str = Depends(authenticate_admin_request)
    ):
        return operation(
            lambda: store.update(
                task,
                body.revision,
                body.enabled,
                body.model_id,
                body.benchmark_id,
                actor,
                body.fallback_model_id,
                body.fallback_benchmark_id,
            )
        )

    @router.post("/benchmarks")
    def benchmark(
        body: Diagnostic,
        request: Request,
        actor: str = Depends(authenticate_admin_request),
    ):
        if not busy.acquire(blocking=False):
            raise HTTPException(409, "A diagnostic is already running. Try again later")
        try:
            connection, serving, result = operation(
                lambda: run_diagnostic(
                    connections,
                    body.model_id,
                    body.task,
                    body.connection_revision,
                )
            )
            authenticate_admin_request(request)
            return operation(
                lambda: store.record(connection, serving, body.task, result, actor)
            )
        finally:
            busy.release()

    return router
