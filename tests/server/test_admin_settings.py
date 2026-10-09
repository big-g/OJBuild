"""Admin isolation, persisted bootstrap overrides and live extraction binding."""

import json

import pytest
from fastapi.testclient import TestClient

from openjarvis.core.admin_settings import AdminSettingsStore, fields, settings_path
from openjarvis.core.config import JarvisConfig, load_config
from openjarvis.memory.extractor import FactExtractor
from openjarvis.memory.service import MemoryService
from openjarvis.memory.store import LocalFactStore
from openjarvis.server.app import create_app
from openjarvis.server.auth_store import AuthStore
from tests.server.test_configured_model_selection import DefaultEngine


class Engine(DefaultEngine):
    def list_models(self):
        return ["qwen3.5:9b", "ornith-1.5:35b"]


@pytest.fixture
def runtime(tmp_path):
    cfg = JarvisConfig()
    cfg._config_dir = tmp_path
    cfg.intelligence.default_model = "qwen3.5:9b"
    cfg.security.runtime_tools_db_path = str(tmp_path / "tools.db")
    cfg.security.model_connections_db_path = str(tmp_path / "models.db")
    cfg.security.generated_files_dir = str(tmp_path / "files")
    cfg.channel.telegram.bot_token = "PRIVATE_TEST_TOKEN"
    engine = Engine()
    extractor = FactExtractor(engine, "qwen3.5:9b")
    memory = MemoryService(LocalFactStore(tmp_path / "facts.jsonl"), extractor)
    app = create_app(engine, "qwen3.5:9b", config=cfg, memory_service=memory)
    auth = AuthStore(tmp_path / "auth.db")
    auth.create_user("admin", "admin", "test-password", is_admin=True)
    auth.create_user("user", "user", "test-password")
    app.state.auth_store = auth
    headers = {
        uid: {"X-OpenJarvis-Session": auth.create_session(uid)}
        for uid in ("admin", "user")
    }
    client = TestClient(app, headers=headers["admin"])
    return app, client, headers, engine, extractor


def save(client, changes, revision=None):
    if revision is None:
        revision = client.get("/v1/admin-settings").json()["revision"]
    return client.put(
        "/v1/admin-settings", json={"revision": revision, "changes": changes}
    )


def test_defaults_apply_without_restart_and_keep_explicit_selection(runtime):
    app, client, _, engine, extractor = runtime
    response = save(client, {"intelligence.default_model": "ornith-1.5:35b"})
    assert response.status_code == 200
    assert app.state.model == "ornith-1.5:35b"
    assert app.state.config.server.model == "ornith-1.5:35b"
    extractor.extract("User preference")
    assert engine.calls[-1] == "ornith-1.5:35b"
    for model, expected in [
        ("default", "ornith-1.5:35b"),
        ("qwen3.5:9b", "qwen3.5:9b"),
    ]:
        result = client.post(
            "/v1/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": "hello"}]},
        )
        assert result.status_code == 200
        assert engine.calls[-1] == expected
    catalog = client.get("/v1/models").json()["data"]
    assert [m["id"] for m in catalog if m["is_default"]] == ["ornith-1.5:35b"]


def test_memory_override_and_follow_default(runtime):
    _, client, _, engine, extractor = runtime
    save(
        client,
        {
            "intelligence.default_model": "ornith-1.5:35b",
            "tools.storage.extraction_model": "qwen3.5:9b",
        },
    )
    extractor.extract("preference")
    assert engine.calls[-1] == "qwen3.5:9b"
    save(client, {"tools.storage.extraction_model": ""})
    extractor.extract("preference")
    assert engine.calls[-1] == "ornith-1.5:35b"


def test_ordinary_users_cannot_read_or_write_and_secrets_are_hidden(runtime):
    _, client, headers, _, _ = runtime
    raw = client.get("/v1/admin-settings").text
    assert "PRIVATE_TEST_TOKEN" not in raw
    protected = next(
        f for f in json.loads(raw)["fields"] if f["key"] == "channel.telegram.bot_token"
    )
    assert protected["editable"] is False
    for method in ("get", "put"):
        kwargs = {} if method == "get" else {"json": {"revision": 1, "changes": {}}}
        assert (
            getattr(client, method)(
                "/v1/admin-settings", headers=headers["user"], **kwargs
            ).status_code
            == 403
        )
    assert (
        save(client, {"channel.telegram.bot_token": "rejected-secret"}).status_code
        == 400
    )
    assert "rejected-secret" not in client.get("/v1/admin-settings").text


def test_revision_conflict_and_validation_are_atomic(runtime):
    app, client, _, _, _ = runtime
    assert save(client, {"intelligence.default_model": "missing"}).status_code == 400
    assert save(client, {"tools.storage.max_facts": True}).status_code == 400
    assert save(client, {"tools.storage.max_facts": 10}, revision=1).status_code == 200
    assert (
        save(
            client, {"intelligence.default_model": "ornith-1.5:35b"}, revision=1
        ).status_code
        == 409
    )
    assert app.state.model == "qwen3.5:9b"
    assert app.state.config.memory.max_facts == 1000
    field = next(
        f
        for f in client.get("/v1/admin-settings").json()["fields"]
        if f["key"] == "tools.storage.max_facts"
    )
    assert (
        field["pending_restart"]
        and field["value"] == 10
        and field["active_value"] == 1000
    )


def test_saved_overrides_survive_restart_and_reset(tmp_path, monkeypatch):
    from openjarvis.core.config import HardwareInfo

    monkeypatch.setattr(
        "openjarvis.core.config.detect_hardware", lambda: HardwareInfo()
    )
    path = tmp_path / "config.toml"
    path.write_text(
        '[intelligence]\ndefault_model="qwen3.5:9b"\n[memory]\nmax_facts=99\n'
    )
    config = load_config(path)
    store = AdminSettingsStore(settings_path(config))
    store.update(
        1,
        {
            "intelligence.default_model": "ornith-1.5:35b",
            "server.model": "ornith-1.5:35b",
            "tools.storage.max_facts": 20,
        },
        "admin",
        fields(config),
    )
    load_config.cache_clear()  # simulate a fresh server process
    restored = load_config(path)
    assert restored.intelligence.default_model == "ornith-1.5:35b"
    assert restored.server.model == "ornith-1.5:35b"
    assert restored.memory.max_facts == 20
    assert restored._settings_bootstrap.memory.max_facts == 99
    store.update(2, {"tools.storage.max_facts": None}, "admin", fields(restored))
    load_config.cache_clear()
    assert load_config(path).memory.max_facts == 99
    assert path.read_text().endswith("max_facts=99\n")


def test_reset_restores_default_and_rejects_wrong_lists_and_ranges(runtime):
    app, client, _, engine, extractor = runtime
    assert (
        save(client, {"intelligence.default_model": "ornith-1.5:35b"}).status_code
        == 200
    )
    assert save(client, {"intelligence.default_model": None}).status_code == 200
    assert app.state.model == "qwen3.5:9b"
    extractor.extract("preference")
    assert engine.calls[-1] == "qwen3.5:9b"
    assert save(client, {"tools.storage.max_facts": 0}).status_code == 400
    assert save(client, {"tools.allowed_tools": [123]}).status_code == 400
    assert save(client, {"intelligence.default_model": ""}).status_code == 400
    assert save(client, {"not.a.setting": 1}).status_code == 400


def test_float_override_retains_its_declared_type_across_restart(tmp_path, monkeypatch):
    from openjarvis.core.config import HardwareInfo

    monkeypatch.setattr(
        "openjarvis.core.config.detect_hardware", lambda: HardwareInfo()
    )
    path = tmp_path / "config.toml"
    path.write_text("")
    config = load_config(path)
    store = AdminSettingsStore(settings_path(config))
    store.update(1, {"intelligence.temperature": 0}, "admin", fields(config))
    load_config.cache_clear()
    restored = load_config(path)
    assert type(restored.intelligence.temperature) is float
    assert fields(restored)["intelligence.temperature"]["type"] == "float"
    store.update(2, {"intelligence.temperature": 0.6}, "admin", fields(restored))
