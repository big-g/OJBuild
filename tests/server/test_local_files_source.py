"""Local source API uses adapter configuration rather than private fields."""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.connectors.local_files import LocalFilesConnector
from openjarvis.core.registry import ConnectorRegistry
from openjarvis.server import connectors_router


def test_local_source_connect_persists_and_survives_new_instance(tmp_path, monkeypatch):
    root = tmp_path / "documents"
    root.mkdir()
    config = tmp_path / "connection.json"
    source = LocalFilesConnector(config_path=str(config))
    ConnectorRegistry.register("local_files")(LocalFilesConnector)
    monkeypatch.setattr(connectors_router, "_instances", {"local_files": source})
    app = FastAPI()
    app.include_router(connectors_router.create_connectors_router())
    with TestClient(app) as client:
        response = client.post(
            "/v1/connectors/local_files/connect", json={"path": str(root)}
        )
        assert response.status_code == 200
        assert response.json()["connected"]
        assert json.loads(config.read_text())["path"] == str(root)
        assert LocalFilesConnector(config_path=str(config)).is_connected()
        listing = client.get("/v1/connectors").json()["connectors"]
        assert any(c["connector_id"] == "local_files" for c in listing)


def test_local_source_rejects_missing_or_invalid_folder(tmp_path, monkeypatch):
    source = LocalFilesConnector(config_path=str(tmp_path / "connection.json"))
    ConnectorRegistry.register("local_files")(LocalFilesConnector)
    monkeypatch.setattr(connectors_router, "_instances", {"local_files": source})
    app = FastAPI()
    app.include_router(connectors_router.create_connectors_router())
    with TestClient(app) as client:
        assert (
            client.post("/v1/connectors/local_files/connect", json={}).status_code
            == 400
        )
        assert (
            client.post(
                "/v1/connectors/local_files/connect",
                json={"path": str(tmp_path / "missing")},
            ).status_code
            == 400
        )
    assert not source.is_connected()
