"""Authentication API routes for OpenJarvis."""

from __future__ import annotations

from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException, Request

from openjarvis.server.auth import get_auth_store


router = APIRouter(prefix="/v1/auth", tags=["authentication"])


class LoginRequest(BaseModel):
    username: str
    password: str = Field(min_length=1)


@router.post("/login")
async def login(req: LoginRequest, request: Request):
    """Authenticate a user and create a server-side session."""
    store = get_auth_store(request)

    user = store.authenticate(
        username=req.username,
        password=req.password,
    )

    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password",
        )

    token = store.create_session(
        user_id=str(user["user_id"]),
    )

    return {
        "user_id": str(user["user_id"]),
        "username": str(user["username"]),
        "display_name": str(user["display_name"]),
        "session_token": token,
    }

@router.get("/me")
async def me(request: Request):
    """Return the currently authenticated user."""
    store = get_auth_store(request)
    token = request.headers.get("X-OpenJarvis-Session", "").strip()

    if not token:
        raise HTTPException(
            status_code=401,
            detail="Missing OpenJarvis session",
        )

    user = store.get_user_for_token(token)

    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired OpenJarvis session",
        )

    return {
        "user_id": str(user["user_id"]),
        "username": str(user["username"]),
        "display_name": str(user["display_name"]),
    }

@router.post("/logout")
async def logout(request: Request):
    """Revoke the current authentication session."""
    token = request.headers.get("X-OpenJarvis-Session", "").strip()

    if not token:
        raise HTTPException(
            status_code=401,
            detail="Missing OpenJarvis session",
        )

    store = get_auth_store(request)
    store.revoke_session(token)

    return {"ok": True}
