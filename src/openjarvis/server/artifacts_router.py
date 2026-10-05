"""Authenticated generated-file endpoints: owner-scoped, never public static files."""

from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from openjarvis.artifacts.preview import preview
from openjarvis.artifacts.store import (
    MAX_REQUEST_BYTES,
    ArtifactError,
    ArtifactNotFound,
    ArtifactStore,
)
from openjarvis.server.auth import authenticate_request

HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
}


def create_artifacts_router(store: ArtifactStore | None) -> APIRouter:
    def available(owner: str = Depends(authenticate_request)):
        if store is None:
            raise HTTPException(
                503, "Private file storage requires a POSIX server filesystem"
            )
        return owner

    router = APIRouter(prefix="/v1/files", tags=["files"])

    def failure(exc):
        return HTTPException(
            404 if isinstance(exc, ArtifactNotFound) else 400, str(exc)
        )

    @router.get("")
    def list_files(owner: str = Depends(available)):
        return JSONResponse({"files": store.list(owner)}, headers=HEADERS)

    @router.post("", status_code=201)
    async def save_file(request: Request, owner: str = Depends(available)):
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                raise HTTPException(413, "File request is too large")
            body.extend(chunk)
        try:
            values = json.loads(body)
            if not isinstance(values, dict) or set(values) - {
                "filename",
                "content",
                "encoding",
            }:
                raise ArtifactError("Expected filename, content and optional encoding")
            result = store.save(
                owner,
                values.get("filename"),
                values.get("content"),
                values.get("encoding", "utf8"),
            )
        except (ArtifactError, json.JSONDecodeError, UnicodeError) as exc:
            raise failure(exc) from exc
        return JSONResponse(result, status_code=201, headers=HEADERS)

    @router.get("/{file_id}/preview")
    def preview_file(file_id: str, owner: str = Depends(available)):
        try:
            metadata, data = store.read(owner, file_id)
        except ArtifactError as exc:
            raise failure(exc) from exc
        return JSONResponse(
            {**metadata, **preview(metadata["filename"], data)}, headers=HEADERS
        )

    @router.get("/{file_id}/download")
    def download_file(file_id: str, owner: str = Depends(available)):
        try:
            metadata, data = store.read(owner, file_id)
        except ArtifactError as exc:
            raise failure(exc) from exc
        headers = {
            **HEADERS,
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + quote(metadata["filename"], safe=""),
        }
        return Response(data, media_type="application/octet-stream", headers=headers)

    @router.delete("/{file_id}", status_code=204)
    def delete_file(file_id: str, owner: str = Depends(available)):
        try:
            store.delete(owner, file_id)
        except ArtifactError as exc:
            raise failure(exc) from exc
        return Response(status_code=204, headers=HEADERS)

    return router
