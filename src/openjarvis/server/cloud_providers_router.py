"""Human-administrator cloud provider key management; secrets are write-only."""

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictInt

from openjarvis.connectors.source_store import SourceConflict
from openjarvis.core.cloud_providers import (
    ProviderConflict,
    change_key,
    public_settings,
)
from openjarvis.server.auth import authenticate_admin_request, authenticate_request


class ProviderSecretSafeRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe(request):
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(
                    422, "Invalid provider request; check revision and key format"
                ) from None

        return safe


router = APIRouter(
    prefix="/v1/cloud/providers",
    tags=["cloud providers"],
    dependencies=[Depends(authenticate_admin_request)],
    route_class=ProviderSecretSafeRoute,
)


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=0)
    key: SecretStr = Field(min_length=1, max_length=8192)


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=0)


def perform(fn):
    try:
        return fn()
    except (ProviderConflict, SourceConflict):
        raise HTTPException(
            409, "Provider settings changed; reload before saving"
        ) from None
    except ValueError:
        raise HTTPException(
            400, "Cannot update provider. Check key format and vault availability."
        ) from None


@router.get("")
def settings(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return perform(public_settings)


@router.put("/{provider}")
def save(
    provider: str,
    body: Edit,
    response: Response,
    actor: str = Depends(authenticate_admin_request),
):
    response.headers["Cache-Control"] = "no-store"
    return perform(
        lambda: change_key(provider, body.key.get_secret_value(), body.revision, actor)
    )


@router.delete("/{provider}")
def remove(
    provider: str,
    body: Revision,
    response: Response,
    actor: str = Depends(authenticate_admin_request),
):
    response.headers["Cache-Control"] = "no-store"
    return perform(lambda: change_key(provider, None, body.revision, actor))


# Model availability is useful to every signed-in account. Credential settings
# and audit records stay on the separate administrator-only router above.

availability_router = APIRouter(
    prefix="/v1/cloud",
    tags=["cloud models"],
    dependencies=[Depends(authenticate_request)],
)


@availability_router.get("/availability")
def availability(response: Response):
    from openjarvis.core.cloud_providers import PROVIDERS, resolve_keys

    response.headers["Cache-Control"] = "no-store"
    keys = resolve_keys()
    return {
        "providers": [
            {"id": identity, "key_name": key, "configured": bool(keys.get(key))}
            for identity, _, key in PROVIDERS
        ]
    }
