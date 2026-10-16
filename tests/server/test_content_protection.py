"""Age policy boundaries, quarantine and administrator decisions end to end."""

import io
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from openjarvis.artifacts.store import ArtifactStore
from openjarvis.server.account_admin_router import router as accounts
from openjarvis.server.artifacts_router import create_artifacts_router
from openjarvis.server.auth_store import AuthStore
from openjarvis.server.content_protection import ContentProtection
from openjarvis.server.content_review_router import router as reviews
from openjarvis.server.workflows_router import create_workflows_router
from openjarvis.workflow.jobs import WorkflowJobs


@pytest.fixture
def env(tmp_path, monkeypatch):
    app = FastAPI()
    store = AuthStore(tmp_path / "auth.db")
    app.state.auth_store = store
    clients = {}
    for name, age, admin in [
        ("admin", 40, True),
        ("kid", 12, False),
        ("adult", 18, False),
        ("unknown", None, False),
    ]:
        store.create_user(name, name, "password123", is_admin=admin)
        store.set_birthdate(
            "admin", name, None if age is None else f"{2026 - age}-01-01"
        )
        clients[name] = TestClient(
            app, headers={"X-OpenJarvis-Session": store.create_session(name)}
        )
    policy = ContentProtection(store)
    app.state.content_protection = policy
    files = ArtifactStore(tmp_path / "files")
    files.read_guard = policy.check_artifact
    jobs = WorkflowJobs(files)
    app.state.workflow_jobs = jobs
    jobs.content_protection = policy
    jobs.image_access_check = store.image_generation_allowed
    monkeypatch.setattr(jobs, "start_worker", lambda: None)
    from openjarvis.workflow.jobs import ImageInstance

    instance = ImageInstance(
        name="Test", url="http://127.0.0.1:8188", checkpoint="test.safetensors"
    )
    monkeypatch.setattr(jobs, "instance", lambda _: instance)
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, "PNG")
    produced = []
    monkeypatch.setattr(
        "openjarvis.workflow.providers.generate_image",
        lambda *args: produced.append(args) or output.getvalue(),
    )
    jobs.approve("admin", "image_generate_local", True)
    app.include_router(accounts)
    app.include_router(reviews)
    app.include_router(create_artifacts_router(files))
    app.include_router(create_workflows_router(jobs))
    yield app, clients, store, policy, jobs, produced
    jobs.close()


def submit(env, owner="kid"):
    _, clients, _, _, _, _ = env
    saved = (
        clients[owner]
        .post(
            "/v1/workflows",
            json={
                "definition": {
                    "version": 1,
                    "name": "Picture",
                    "steps": [
                        {
                            "id": "image",
                            "operation": "image_generate_local",
                            "parameters": {"instance_id": "local"},
                        }
                    ],
                }
            },
        )
        .json()
    )
    response = clients[owner].post(
        "/v1/workflows/image-runs",
        json={"workflow_id": saved["id"], "revision": 1, "prompt": "A red apple"},
    )
    assert response.status_code == 202, response.text
    return response.json()["id"]


def execute(env, run_id):
    jobs = env[4]
    with jobs.store.db() as db:
        row = dict(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
    jobs._execute(row)
    return jobs.store.run(row["owner"], run_id)


def decision(env, item, approve=True):
    return env[1]["admin"].post(
        "/v1/content-reviews/" + item["id"],
        json={"stage": item["state"], "approve": approve},
    )


@pytest.mark.parametrize("owner", ["kid", "unknown"])
def test_both_stages_required_and_no_image_leaks(env, owner):
    _, clients, _, policy, jobs, produced = env
    run_id = submit(env, owner)
    assert execute(env, run_id)["status"] == "awaiting_review"
    assert not produced
    item = policy.listing()[0]
    assert item["state"] == "pending_request"
    assert clients[owner].get("/v1/content-reviews").status_code == 403
    assert (
        clients[owner]
        .post(
            "/v1/content-reviews/" + item["id"],
            json={"stage": item["state"], "approve": True},
        )
        .status_code
        == 403
    )
    assert decision(env, item).status_code == 200
    assert execute(env, run_id)["status"] == "awaiting_review"
    assert len(produced) == 1
    assert jobs.artifacts.list(owner) == []
    item = policy.listing()[0]
    assert item["state"] == "pending_image"
    url = "/v1/content-reviews/" + item["id"] + "/image"
    assert clients[owner].get(url).status_code == 403
    assert clients["admin"].get(url).headers["content-type"] == "image/png"
    assert decision(env, item).status_code == 200
    run = execute(env, run_id)
    assert run["status"] == "completed", run
    assert len(produced) == 1  # approved output is reused, never regenerated
    file_id = run["output"]["artifact_id"]
    assert clients[owner].get(f"/v1/files/{file_id}/download").status_code == 200
    assert clients["adult"].get(f"/v1/files/{file_id}/download").status_code == 404


def test_adult_path_and_age_change_during_generation(env, monkeypatch):
    assert execute(env, submit(env, "adult"))["status"] == "completed"
    _, _, store, _, jobs, _ = env

    def change_age(*args):
        store.set_birthdate("admin", "adult", "2010-01-01")
        return b"not-released"

    monkeypatch.setattr("openjarvis.workflow.providers.generate_image", change_age)
    run = execute(env, submit(env, "adult"))
    assert run["status"] == "failed"
    assert len(jobs.artifacts.list("adult")) == 1


def test_age_validation_persistence_and_no_self_upgrade(env):
    _, clients, store, policy, _, _ = env
    path = "/v1/auth/users/kid/birthdate"
    assert clients["kid"].put(path, json={"birthdate": "1986-01-01"}).status_code == 403
    for age in [-1, 121, True, "18", 17.5]:
        assert clients["admin"].put(path, json={"birthdate": age}).status_code in {
            400,
            422,
        }
    assert (
        clients["admin"].put(path, json={"birthdate": "2010-01-01"}).status_code == 200
    )
    assert policy.policy("kid")[0]
    assert AuthStore(store._db_path).get_user("kid")["birthdate"] == "2010-01-01"
    assert (
        clients["admin"].put(path, json={"birthdate": "2000-01-01"}).status_code == 200
    )
    assert not policy.policy("kid")[0]
    assert clients["admin"].put(path, json={"birthdate": None}).status_code == 200
    assert policy.policy("kid")[0]
    with store._connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM account_permission_audit WHERE "
                "permission='birthdate_changed'"
            ).fetchone()[0]
            >= 3
        )


def test_rejection_and_changed_age_invalidate_review(env):
    run_id = submit(env)
    execute(env, run_id)
    item = env[3].listing()[0]
    assert decision(env, item, False).status_code == 200
    assert execute(env, run_id)["status"] == "failed"
    assert not env[5]
    run_id = submit(env)
    execute(env, run_id)
    item = env[3].listing()[0]
    env[2].set_birthdate("admin", "kid", "2016-01-01")
    assert decision(env, item).status_code == 409
    assert execute(env, run_id)["status"] == "awaiting_review"
    assert not env[5]


def test_protection_is_independent_of_direct_permission_and_chat_tools(env):
    from openjarvis.core.correlation import ExecutionIdentity, execution_scope
    from openjarvis.tools.workflow import WorkflowChatTool

    run_id = submit(env)
    jobs = env[4]
    run = jobs.store.run("kid", run_id)
    env[2].set_image_generation("admin", "kid", False)
    with execution_scope(ExecutionIdentity(user_id="kid")):
        result = WorkflowChatTool(jobs, "workflow_submit").execute(
            workflow_id=run["workflow_id"], revision=1, prompt="A green apple"
        )
    assert result.success
    assert execute(env, json.loads(result.content)["id"])["status"] == "awaiting_review"
    assert not env[5]


def test_age_downgrade_blocks_old_images_and_unreviewable_3d(env):
    run = execute(env, submit(env, "adult"))
    env[2].set_birthdate("admin", "adult", "2011-01-01")
    path = "/v1/files/" + run["output"]["artifact_id"]
    from openjarvis.artifacts.store import ArtifactAccessDenied

    with pytest.raises(ArtifactAccessDenied):
        env[4].artifacts.read("adult", run["output"]["artifact_id"])
    assert env[1]["adult"].get(path + "/download").status_code == 403
    assert env[1]["adult"].get(path + "/preview").status_code == 403
    with pytest.raises(ValueError, match="3D generation"):
        env[4].perform("adult", "hunyuan_generate", {}, {})


def test_non_adult_admin_cannot_preview_or_approve(env):
    run_id = submit(env)
    execute(env, run_id)
    item = env[3].listing()[0]
    env[2].set_birthdate("admin", "admin", None)
    assert env[1]["admin"].get("/v1/content-reviews").status_code == 403
    assert decision(env, item).status_code == 403


@pytest.mark.parametrize(
    "instant,expected",
    [
        ("2026-10-16T03:59:59+00:00", 17),
        ("2026-10-16T04:00:00+00:00", 18),
    ],
)
def test_birthday_at_local_midnight(instant, expected):
    from datetime import datetime

    user = {"birthdate": "2008-10-16", "birthdate_timezone": "America/New_York"}
    assert AuthStore.user_age(user, datetime.fromisoformat(instant)) == expected


def test_leap_birthday_and_unknown_birthdate():
    from datetime import datetime, timezone

    user = {"birthdate": "2008-02-29", "birthdate_timezone": "UTC"}
    assert AuthStore.user_age(user, datetime(2026, 2, 28, tzinfo=timezone.utc)) == 17
    assert AuthStore.user_age(user, datetime(2026, 3, 1, tzinfo=timezone.utc)) == 18
    assert AuthStore.user_age({"birthdate": None}) is None


def test_turning_eighteen_resumes_waiting_run_without_edit(env, monkeypatch):
    from datetime import datetime, timezone

    import openjarvis.server.auth_store as auth_module

    class Clock(datetime):
        instant = datetime(2026, 10, 16, 3, 59, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz or timezone.utc)

    monkeypatch.setattr(auth_module, "datetime", Clock)
    env[2].set_birthdate("admin", "kid", "2008-10-16", "America/New_York")
    run_id = submit(env)
    assert execute(env, run_id)["status"] == "awaiting_review"
    assert not env[5]
    revision = env[2].get_user("kid")["content_revision"]
    Clock.instant = datetime(2026, 10, 16, 4, 0, tzinfo=timezone.utc)
    env[4].poll_triggers()
    assert env[4].store.run("kid", run_id)["status"] == "queued"
    assert execute(env, run_id)["status"] == "completed"
    assert env[2].get_user("kid")["content_revision"] == revision
    assert env[3].listing() == []


def test_birthdate_validation_before_account_creation(env):
    admin = env[1]["admin"]
    for birthdate, zone in [
        ("2100-01-01", "UTC"),
        ("2008-02-30", "UTC"),
        ("2008-01-01", "Invalid/Zone"),
        ("1899-01-01", "UTC"),
    ]:
        response = admin.post(
            "/v1/auth/users",
            json={
                "username": "invalid",
                "display_name": "Invalid",
                "password": "password123",
                "birthdate": birthdate,
                "birthdate_timezone": zone,
            },
        )
        assert response.status_code == 400
        assert env[2].get_user_by_username("invalid") is None
    response = admin.post(
        "/v1/auth/users",
        json={
            "username": "new",
            "display_name": "New",
            "password": "password123",
            "birthdate": "2015-10-16",
            "birthdate_timezone": "America/New_York",
        },
    )
    assert response.status_code == 201
    assert env[3].policy(response.json()["user_id"])[0]


def test_image_rejection_duplicate_decision_and_cancel(env):
    run_id = submit(env)
    execute(env, run_id)
    item = env[3].listing()[0]
    assert decision(env, item).status_code == 200
    assert decision(env, item).status_code == 409
    execute(env, run_id)
    item = env[3].listing()[0]
    assert decision(env, item, False).status_code == 200
    assert execute(env, run_id)["status"] == "failed"
    assert env[4].artifacts.list("kid") == []
    run_id = submit(env)
    execute(env, run_id)
    item = env[3].listing()[0]
    assert env[1]["kid"].post(f"/v1/workflows/runs/{run_id}/cancel").status_code == 200
    assert decision(env, item).status_code == 409
    assert env[4].store.run("kid", run_id)["status"] == "cancelled"


def test_review_survives_restart_and_bad_output_fails_closed(env, monkeypatch):
    run_id = submit(env)
    execute(env, run_id)
    restored = ContentProtection(AuthStore(env[2]._db_path))
    item = restored.listing()[0]
    assert decision(env, item).status_code == 200
    monkeypatch.setattr(
        "openjarvis.workflow.providers.generate_image", lambda *args: b"invalid"
    )
    assert execute(env, run_id)["status"] == "failed"
    assert env[4].artifacts.list("kid") == []
    assert restored.listing() == []


def test_protected_3d_http_and_workflow_import_are_blocked(env):
    from openjarvis.server.hunyuan_router import create_hunyuan_router

    env[0].include_router(create_hunyuan_router(None))
    assert env[1]["kid"].post("/v1/3d/jobs", content=b"image").status_code == 403
    assert env[1]["kid"].get("/v1/3d/jobs/anything/download").status_code == 403
    with pytest.raises(ValueError, match="3D generation"):
        env[4].import_hunyuan("kid", "anything")


def test_review_expiration_clears_bytes_and_fails_run(env):
    import time

    run_id = submit(env)
    execute(env, run_id)
    item = env[3].listing()[0]
    with env[2]._connect() as db:
        db.execute(
            "UPDATE content_reviews SET created=? WHERE id=?",
            (time.time() - 8 * 86400, item["id"]),
        )
    env[4].poll_triggers()
    assert execute(env, run_id)["status"] == "failed"
    assert env[3].listing() == []
    assert not env[5]


def test_old_accounts_migrate_to_unknown_birthdate(tmp_path):
    path = tmp_path / "auth.db"
    store = AuthStore(path)
    store.create_user("admin", "admin", "password123", is_admin=True)
    store.set_image_generation("admin", "admin", False)
    with store._connect() as db:
        for column in ("birthdate", "birthdate_timezone", "content_revision"):
            db.execute("ALTER TABLE users DROP COLUMN " + column)
    migrated = AuthStore(path)
    assert migrated.user_age(migrated.get_user("admin")) is None
    assert not migrated.image_generation_allowed("admin")
    assert ContentProtection(migrated).policy("admin")[0]
    assert path.stat().st_mode & 0o777 == 0o600
