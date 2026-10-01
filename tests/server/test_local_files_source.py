"""Legacy endpoints cannot mutate independently owned Local Files instances."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.connectors.local_files import LocalFilesConnector
from openjarvis.core.registry import ConnectorRegistry
from openjarvis.server.connectors_router import create_connectors_router


@pytest.mark.parametrize("action", ["connect", "sync", "disconnect"])
def test_local_files_lifecycle_uses_instance_api(action):
    ConnectorRegistry.register("local_files")(LocalFilesConnector)
    app = FastAPI()
    app.include_router(create_connectors_router())
    with TestClient(app) as client:
        response = client.post(f"/v1/connectors/local_files/{action}", json={})
        assert response.status_code == 409
        assert "/v1/sources" in response.json()["detail"]
