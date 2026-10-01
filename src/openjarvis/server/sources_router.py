"""Authenticated web management of named, database-backed source instances."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from openjarvis.connectors.source_adapters import list_adapters
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


def create_sources_router(manager: SourceManager | None = None) -> APIRouter:
    manager = manager or SourceManager()
    router = APIRouter(prefix="/v1/sources", tags=["sources"])

    def invoke(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except KeyError as exc:
            raise HTTPException(404, "Source not found") from exc
        except SourceConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

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
    def create_source(req: SourceInput):
        result = invoke(manager.create, req.adapter_id, req.name, req.config)
        result.pop("legacy_document_ids")
        return result

    @router.put("/{source_id}")
    def edit_source(source_id: str, req: SourceEdit):
        result = invoke(
            manager.update,
            source_id,
            req.revision,
            name=req.name,
            config=req.config,
            enabled=req.enabled,
        )
        result.pop("legacy_document_ids")
        return result

    @router.delete("/{source_id}", status_code=204)
    def remove_source(source_id: str, revision: int):
        invoke(manager.delete, source_id, revision)

    @router.post("/{source_id}/sync", status_code=202)
    def sync_source(source_id: str):
        invoke(manager.start_sync, source_id)
        return {"source_id": source_id, "status": "started"}

    return router
