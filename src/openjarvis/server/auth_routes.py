"""Authentication API routes for OpenJarvis."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

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
        "is_admin": bool(user["is_admin"]),
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
        "is_admin": bool(user["is_admin"]),
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


class PasswordProof(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)


class PasswordChange(PasswordProof):
    new_password: str = Field(min_length=8, max_length=1024)


class AccountRecovery(BaseModel):
    recovery_code: str = Field(min_length=32, max_length=256)
    new_password: str | None = Field(default=None, min_length=8, max_length=1024)


def _private_response(data):
    from fastapi.responses import JSONResponse

    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@router.post("/password")
async def change_password(req: PasswordChange, request: Request):
    from openjarvis.server.auth import get_authenticated_user_id

    user_id = get_authenticated_user_id(request)
    try:
        get_auth_store(request).change_password(
            user_id, req.current_password, req.new_password
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _private_response({"ok": True})


@router.post("/recovery-code")
async def issue_recovery_code(req: PasswordProof, request: Request):
    from openjarvis.server.auth import get_authenticated_user_id

    user_id = get_authenticated_user_id(request)
    try:
        result = get_auth_store(request).issue_recovery_code(
            user_id, req.current_password
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _private_response(result)


@router.post("/recover")
async def recover_account(req: AccountRecovery, request: Request):
    try:
        username = get_auth_store(request).recover_account(
            req.recovery_code, req.new_password
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _private_response(
        {"username": username, "password_reset": req.new_password is not None}
    )
