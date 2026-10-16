"""Administrator-only content review. No image bytes are exposed to the owner."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from openjarvis.server.auth import authenticate_admin_request, get_auth_store

router = APIRouter(prefix="/v1/content-reviews", tags=["content protection"])


def reviewer(request: Request, actor=Depends(authenticate_admin_request)):
    user = get_auth_store(request).get_user(actor)
    age = get_auth_store(request).user_age(user)
    if age is None or age < 18:
        raise HTTPException(
            403,
            "Content review requires an administrator with a birthdate "
            "establishing age 18 or older",
        )
    return actor


def protection(request):
    service = getattr(request.app.state, "content_protection", None)
    if service is None:
        raise HTTPException(503, "Content review is unavailable")
    return service


@router.get("")
def listing(request: Request, response: Response, actor=Depends(reviewer)):
    response.headers["Cache-Control"] = "no-store"
    return {"reviews": protection(request).listing()}


@router.get("/{review_id}/image")
def preview(review_id: str, request: Request, actor=Depends(reviewer)):
    try:
        data = protection(request).preview(review_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    return Response(
        data,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    stage: Literal["pending_request", "pending_image"]
    approve: bool


@router.post("/{review_id}")
def decide(review_id: str, body: Decision, request: Request, actor=Depends(reviewer)):
    try:
        owner, run_id = protection(request).decide(
            actor, review_id, body.stage, body.approve
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    jobs = getattr(request.app.state, "workflow_jobs", None)
    if jobs is not None:
        jobs.resume_review(owner, run_id)
    return {"ok": True}
