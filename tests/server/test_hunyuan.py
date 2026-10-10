import json
import uuid

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.core.config import JarvisConfig
from openjarvis.server.auth_middleware import AuthMiddleware
from openjarvis.server.auth_store import AuthStore
from openjarvis.server.hunyuan_router import HunyuanJobs, create_hunyuan_router


@pytest.fixture
def setup(tmp_path):
    app = FastAPI()
    app.state.auth_store = AuthStore(tmp_path / "auth.db")
    headers = {}
    for owner in ("alice", "bob"):
        app.state.auth_store.create_user(owner, owner, "password123")
        headers[owner] = {
            "X-OpenJarvis-Session": app.state.auth_store.create_session(owner)
        }
    jobs = HunyuanJobs(
        tmp_path / "private/jobs.db", tmp_path / "inputs", "http://127.0.0.1:8090"
    )
    config = JarvisConfig()
    config.server.model = "qwen3.5:9b"
    config.server.generation_chat_model = "qwen3.5:2b"
    app.include_router(create_hunyuan_router(jobs, config))
    app.add_middleware(AuthMiddleware, api_key="master")
    return TestClient(app), headers, jobs


@respx.mock
def test_submit_status_download_owner_and_no_path_leak(setup):
    client, headers, jobs = setup
    job_id = str(uuid.uuid4())

    def accept(request):
        values = json.loads(request.content)
        from pathlib import Path

        assert Path(values["image_path"]).read_bytes().startswith(b"\x89PNG")
        assert values["model"] == "turbo"
        assert values["primary_model"] == "qwen3.5:9b"
        assert values["background_model"] == "qwen3.5:2b"
        return httpx.Response(
            202, json={"job_id": job_id, "status": "running", "model": "turbo"}
        )

    respx.post(jobs.url + "/jobs").mock(side_effect=accept)
    response = client.post(
        "/v1/3d/jobs?model=turbo",
        headers=headers["alice"],
        content=b"\x89PNG\r\n\x1a\nimage",
    )
    assert response.status_code == 202, response.text
    assert not list(jobs.inputs.iterdir()), "Staging image must be removed"
    respx.get(jobs.url + "/jobs/" + job_id).respond(
        200,
        json={
            "job_id": job_id,
            "status": "completed",
            "output": "/private/output.glb",
            "watertight": False,
        },
    )
    result = client.get("/v1/3d/jobs/" + job_id, headers=headers["alice"])
    assert result.json()["watertight"] is False and "output" not in result.json()
    assert client.get("/v1/3d/jobs", headers=headers["bob"]).json() == {
        "jobs": [],
        "default_model": "turbo",
    }
    for suffix in ("", "/download"):
        assert (
            client.get(
                "/v1/3d/jobs/" + job_id + suffix, headers=headers["bob"]
            ).status_code
            == 404
        )
    data = b"glTF" + b"\x02\0\0\0" + b"\x0c\0\0\0"
    respx.get(jobs.url + "/jobs/" + job_id + "/download").respond(200, content=data)
    result = client.get("/v1/3d/jobs/" + job_id + "/download", headers=headers["alice"])
    assert result.content == data and result.headers["cache-control"] == "no-store"


@respx.mock
def test_auth_bad_input_and_worker_failure(setup, monkeypatch):
    client, headers, jobs = setup
    for h in ({}, {"Authorization": "Bearer master"}):
        assert client.get("/v1/3d/jobs", headers=h).status_code == 401
    assert (
        client.post(
            "/v1/3d/jobs", headers=headers["alice"], content=b"not image"
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/v1/3d/jobs?model=unknown", headers=headers["alice"], content=b"x"
        ).status_code
        == 422
    )
    from openjarvis.server import hunyuan_router

    monkeypatch.setattr(hunyuan_router, "MAX_IMAGE_BYTES", 3)
    assert (
        client.post(
            "/v1/3d/jobs", headers=headers["alice"], content=b"1234"
        ).status_code
        == 413
    )
    monkeypatch.setattr(hunyuan_router, "MAX_IMAGE_BYTES", 1024)
    respx.post(jobs.url + "/jobs").respond(409)
    result = client.post(
        "/v1/3d/jobs", headers=headers["alice"], content=b"\x89PNG\r\n\x1a\nimage"
    )
    assert result.status_code == 409 and not list(jobs.inputs.iterdir())


@respx.mock
def test_failed_state_is_sanitized_and_not_downloadable(setup):
    client, headers, jobs = setup
    job_id = str(uuid.uuid4())
    with jobs.db() as db:
        db.execute("INSERT INTO jobs VALUES (?,?)", (job_id, "alice"))
    respx.get(jobs.url + "/jobs/" + job_id).respond(
        200,
        json={
            "job_id": job_id,
            "status": "failed",
            "error": "/private/traceback",
        },
    )
    response = client.get("/v1/3d/jobs/" + job_id, headers=headers["alice"])
    assert "/private" not in response.text
    assert (
        client.get(
            "/v1/3d/jobs/" + job_id + "/download", headers=headers["alice"]
        ).status_code
        == 409
    )


def test_reject_nonlocal_worker_and_symlink_input_root(tmp_path, setup):
    with pytest.raises(ValueError):
        HunyuanJobs(tmp_path / "db", tmp_path / "inputs", "http://example.org")
    client, headers, jobs = setup
    target = tmp_path / "target"
    target.mkdir()
    jobs.inputs.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        client.post(
            "/v1/3d/jobs", headers=headers["alice"], content=b"\x89PNG\r\n\x1a\nimage"
        )
    assert not list(target.iterdir())
