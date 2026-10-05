"""Explicit account roles managed only by existing human administrators."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from openjarvis.server.auth import authenticate_admin_request, get_auth_store
from openjarvis.server.runtime_mcp_router import SecretSafeRoute


class Role(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "administrator"]


class CreateAccount(Role):
    role: Literal["user", "administrator"] = "user"
    username: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    password: SecretStr = Field(min_length=8, max_length=1024)


router = APIRouter(
    prefix="/v1/auth/users",
    tags=["account management"],
    dependencies=[Depends(authenticate_admin_request)],
    route_class=SecretSafeRoute,
)


def operation(fn):
    try:
        return fn()
    except PermissionError:
        raise HTTPException(403, "Administrator account required") from None
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Username already exists") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get("")
def list_accounts(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"users": get_auth_store(request).list_users()}


@router.post("", status_code=201)
def create_account(
    body: CreateAccount,
    request: Request,
    actor: str = Depends(authenticate_admin_request),
):
    username, display_name = body.username.strip(), body.display_name.strip()
    if (
        not username
        or not display_name
        or not username.isprintable()
        or not display_name.isprintable()
    ):
        raise HTTPException(
            400, "Username and display name must contain printable text"
        )
    store = get_auth_store(request)
    uid = "user_" + uuid.uuid4().hex
    operation(
        lambda: store.create_user(
            uid,
            username,
            body.password.get_secret_value(),
            display_name,
            is_admin=body.role == "administrator",
            actor_id=actor,
        )
    )
    return {
        "user_id": uid,
        "username": username,
        "display_name": display_name,
        "is_admin": body.role == "administrator",
        "disabled": False,
    }


@router.put("/{user_id}/role")
def set_role(
    user_id: str,
    body: Role,
    request: Request,
    actor: str = Depends(authenticate_admin_request),
):
    operation(
        lambda: get_auth_store(request).change_role(
            actor, user_id, body.role == "administrator"
        )
    )
    return {"ok": True}


@router.delete("/{user_id}")
def delete_account(
    user_id: str, request: Request, actor: str = Depends(authenticate_admin_request)
):
    operation(lambda: get_auth_store(request).delete_user(user_id, actor_id=actor))
    return {"ok": True}
