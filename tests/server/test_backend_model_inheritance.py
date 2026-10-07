"""Backend configuration carries into diagnostics without duplicate setup."""

import json

import httpx
import pytest

from openjarvis.core.events import EventBus
from openjarvis.engine.configured_models import selectable_models
from openjarvis.engine.connection_store import ConnectionConflict, ModelConnectionStore
from openjarvis.engine.model_benchmarks import CASES
from openjarvis.engine.multi import MultiEngine
from openjarvis.engine.ollama import OllamaEngine
from openjarvis.security.guardrails import GuardrailsEngine
from openjarvis.telemetry.instrumented_engine import InstrumentedEngine
from tests.server.test_configured_model_selection import setup  # noqa: F401

PATH = "/v1/model-routing/backend-models"


@pytest.fixture
def inherited(setup, monkeypatch):  # noqa: F811
    app, client, headers, _, _, _, _ = setup
    calls = []
    state = {
        "models": ["qwen3.5:9b", "ornith:latest", "coder:7b", "vision:8b"],
        "caps": ["completion"],
        "mutate": None,
        "down": False,
    }
    native = httpx.Client

    def handle(request):
        calls.append(request)
        if state["down"]:
            return httpx.Response(503)
        if state["mutate"]:
            mutation, state["mutate"] = state["mutate"], None
            mutation()
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [{"name": name, "size": 100} for name in state["models"]]
                },
            )
        body = json.loads(request.content)
        assert body["model"] in state["models"]
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": state["caps"]})
        assert request.url.path == "/api/chat"
        prompt = body["messages"][0]["content"]
        answer = next(
            expected
            for cases in CASES.values()
            for _, text, expected in cases
            if text == prompt
        )
        return httpx.Response(
            200,
            json={
                "message": {"content": json.dumps({"answer": answer})},
                "done_reason": "stop",
                "eval_count": 3,
            },
        )

    def factory(**kwargs):
        bound = native(**kwargs)
        bound._transport = httpx.MockTransport(handle)
        return bound

    monkeypatch.setattr(httpx, "Client", factory)
    app.state.engine = OllamaEngine(host="http://192.168.1.42:11434")
    return app, client, headers, calls, state


def sync(inherited):
    result = inherited[1].post(PATH)
    assert result.status_code == 200, result.text
    return result.json()


def test_four_models_selectable_and_diagnostic_assignable(inherited):
    app, client, _, calls, state = inherited
    assert sync(inherited)["changed"] == 1
    store = app.state.model_connection_store
    (row,) = store.list()
    assert row["url"] == "http://192.168.1.42:11434" and row["enabled"]
    assert {m["serving_id"] for m in selectable_models(store)} == set(state["models"])
    assert all(r.url.path in {"/api/tags", "/api/show"} for r in calls)
    selected = selectable_models(store)[0]["id"]
    assert selected in {m["id"] for m in client.get("/v1/models").json()["data"]}
    diagnostic = client.post(
        "/v1/model-routing/benchmarks",
        json={
            "task": "coding",
            "model_id": selected,
            "connection_revision": row["revision"],
        },
    )
    assert diagnostic.status_code == 200, diagnostic.text
    benchmark = diagnostic.json()
    assert benchmark["passed"]
    rule = next(
        r
        for r in client.get("/v1/model-routing").json()["rules"]
        if r["task"] == "coding"
    )
    assert sync(inherited)["changed"] == 0
    assert store.get(row["id"])["revision"] == row["revision"]
    result = client.put(
        "/v1/model-routing/tasks/coding",
        json={
            **{k: rule[k] for k in ("revision",)},
            "enabled": True,
            "model_id": selected,
            "benchmark_id": benchmark["id"],
        },
    )
    assert result.status_code == 200, result.text
    state["models"].append("new:latest")
    assert sync(inherited)["changed"] == 1
    assert store.get(row["id"])["revision"] == row["revision"] + 1
    # Old measurements cannot approve the changed catalog.
    result = client.put(
        "/v1/model-routing/tasks/coding",
        json={
            "revision": result.json()["revision"],
            "enabled": True,
            "model_id": selected,
            "benchmark_id": benchmark["id"],
        },
    )
    assert result.status_code == 409


@pytest.mark.parametrize("action", ["disable", "edit", "remove"])
def test_explicit_overrides_survive_refresh_and_store_reopen(inherited, action):
    app, _, _, calls, _ = inherited
    sync(inherited)
    store = app.state.model_connection_store
    (row,) = store.list()
    if action == "disable":
        store.enable(row["id"], row["revision"], False, "admin")
    elif action == "edit":
        store.update(row["id"], row["revision"], "edited", row["url"], "admin")
    else:
        store.remove(row["id"], row["revision"], "admin")
    ModelConnectionStore(store.path)  # migration/restart preserves tombstones
    calls.clear()
    result = sync(inherited)
    assert result["changed"] == 0 and result["notices"] and not calls
    assert not selectable_models(store)


def test_initial_manual_record_is_reused_but_explicit_disabled_is_respected(inherited):
    app, _, _, _, _ = inherited
    store = app.state.model_connection_store
    row = store.create("home", "http://192.168.1.42:11434", "admin")
    assert sync(inherited)["changed"] == 1
    assert store.list()[0]["id"] == row["id"] and len(store.list()) == 1


def test_preexisting_explicit_disable_is_not_bypassed(inherited):
    app, _, _, calls, _ = inherited
    store = app.state.model_connection_store
    row = store.create("home", "http://192.168.1.42:11434", "admin")
    store.enable(row["id"], row["revision"], False, "admin")
    calls.clear()
    assert sync(inherited)["changed"] == 0 and not calls
    assert len(store.list()) == 1 and not store.list()[0]["enabled"]


def test_runtime_endpoint_and_known_wrappers_are_carried_forward(inherited):
    app, _, _, _, _ = inherited
    engine = app.state.engine
    app.state.config.engine.ollama.host = "http://127.0.0.1:11434"
    wrapped = InstrumentedEngine(GuardrailsEngine(engine, scanners=[]), EventBus())
    app.state.engine = MultiEngine([("first", wrapped), ("same", engine)])
    assert sync(inherited)["changed"] == 1
    assert len(app.state.model_connection_store.list()) == 1
    assert app.state.model_connection_store.list()[0]["url"] == engine._host


def test_missing_manifest_does_not_invent_capabilities(inherited):
    app, _, _, _, state = inherited
    state["caps"] = None
    result = sync(inherited)
    assert result["notices"] and not selectable_models(app.state.model_connection_store)
    state["caps"] = ["completion"]
    assert sync(inherited)["changed"] == 1
    assert len(selectable_models(app.state.model_connection_store)) == 4


def test_nonadmin_cannot_import_or_trigger_metadata(inherited):
    app, client, headers, calls, _ = inherited
    calls.clear()
    assert client.post(PATH, headers=headers["user"]).status_code == 403
    assert not calls and not app.state.model_connection_store.list()


def test_role_revoked_during_metadata_never_commits(inherited):
    app, client, _, _, state = inherited
    state["mutate"] = lambda: app.state.auth_store.set_admin("admin", False)
    result = client.post(PATH)
    assert result.status_code == 403
    assert not app.state.model_connection_store.list()


def test_admin_change_during_metadata_wins(inherited):
    app, client, _, _, state = inherited
    sync(inherited)
    store = app.state.model_connection_store
    (row,) = store.list()
    state["mutate"] = lambda: store.enable(row["id"], row["revision"], False, "admin")
    assert client.post(PATH).status_code == 409
    assert not store.get(row["id"])["enabled"]


def test_invalid_endpoint_never_contacts_provider(inherited):
    app, _, _, calls, _ = inherited
    app.state.engine._host = "https://8.8.8.8"
    calls.clear()
    assert sync(inherited)["notices"] and not calls
    assert not app.state.model_connection_store.list()


def test_catalog_failure_preserves_previous_records(inherited):
    app, _, _, _, state = inherited
    sync(inherited)
    before = app.state.model_connection_store.list()
    state["down"] = True
    result = sync(inherited)
    assert result["changed"] == 0 and result["notices"]
    assert app.state.model_connection_store.list() == before


def test_unknown_engine_does_not_guess_ollama_host(setup):  # noqa: F811
    app, client, _, calls, _, _, _ = setup
    calls.clear()
    assert client.post(PATH).json()["changed"] == 0
    assert not calls and not app.state.model_connection_store.list()


def test_stale_snapshot_cannot_overwrite_manual_create(inherited):
    app, _, _, _, _ = inherited
    store = app.state.model_connection_store
    endpoint = app.state.engine._host
    expected = store.backend_target(endpoint)
    store.create("manual", endpoint, "admin")
    with pytest.raises(ConnectionConflict):
        store.inherit_backend(endpoint, [], expected, "admin")


def test_duplicate_endpoint_cannot_bypass_explicit_admin_override(inherited):
    app, _, _, calls, _ = inherited
    store = app.state.model_connection_store
    endpoint = app.state.engine._host
    store.create("unfinished", endpoint, "admin")
    disabled = store.create("disabled", endpoint, "admin")
    store.enable(disabled["id"], disabled["revision"], False, "admin")
    calls.clear()
    assert sync(inherited)["changed"] == 0 and not calls
    assert not selectable_models(store)


def test_multiple_backend_servers_keep_exact_server_identities(inherited):
    app, _, _, _, _ = inherited
    app.state.engine = MultiEngine(
        [
            ("first", app.state.engine),
            ("second", OllamaEngine(host="http://192.168.1.43:11434")),
        ]
    )
    assert sync(inherited)["changed"] == 2
    models = selectable_models(app.state.model_connection_store)
    assert len(models) == 8 and len({m["id"] for m in models}) == 8
    assert len({m["connection_id"] for m in models}) == 2
    assert sync(inherited)["changed"] == 0


def test_embedding_models_are_visible_but_never_selectable(inherited):
    app, _, _, _, state = inherited
    state["caps"] = ["embedding"]
    assert sync(inherited)["changed"] == 1
    (row,) = app.state.model_connection_store.list()
    assert len(row["catalog"]) == 4 and not row["enabled"]
    assert not selectable_models(app.state.model_connection_store)


def test_manifest_reads_are_bounded(inherited):
    app, _, _, calls, state = inherited
    state["models"] = [f"model:{i}" for i in range(40)]
    calls.clear()
    assert sync(inherited)["notices"]
    assert sum(r.url.path == "/api/show" for r in calls) == 32
    assert len(selectable_models(app.state.model_connection_store)) == 32
