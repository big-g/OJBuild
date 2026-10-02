"""Named account import recipes, bundle recovery and legacy grant preservation."""

import concurrent.futures
import json
from dataclasses import replace

import httpx
import pytest

from openjarvis.connectors import source_adapters
from openjarvis.connectors.instance_sources import (
    ACCOUNT_READERS,
    AccountSource,
    token_path,
)
from openjarvis.connectors.oauth import (
    GOOGLE_ALL_SCOPES,
    connector_scopes,
    delete_tokens,
    get_provider_for_connector,
    load_tokens,
    save_tokens,
)
from openjarvis.connectors.source_audit import list_events
from openjarvis.connectors.source_imports import _IMPORTS, SourceImports
from openjarvis.connectors.source_manager import SourceManager
from openjarvis.connectors.source_store import SourceConflict, SourceStore
from openjarvis.connectors.token_vault import TokenVault


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("Imports must not contact a provider")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)


@pytest.fixture
def manager(tmp_path):
    return SourceManager(
        SourceStore(
            str(tmp_path / "sources.db"), legacy_path=str(tmp_path / "missing")
        ),
        knowledge_path=str(tmp_path / "knowledge.db"),
    )


def payload(service):
    auth = ACCOUNT_READERS[service][3]
    if auth == "oauth":
        provider = get_provider_for_connector(service)
        return {
            "access_token": "private-access-token",
            "refresh_token": "private-refresh-token",
            "client_id": "test-application",
            "client_secret": "private-application-secret",
            "expires_at": 1,
            "expires_in": 3600,
            "requested_scopes": connector_scopes(provider, service),
        }
    if service == "weather":
        return {"api_key": "private-weather-api-key", "location": "Boston"}
    return {
        "token": "xoxp-private-user-token"
        if service == "slack"
        else "private-account-token"
    }


def seed(manager, service, values=None, *, plaintext=False, filename=None):
    path = (
        manager.store.path.parent
        / "connectors"
        / (filename or _IMPORTS[service].filename)
    )
    data = payload(service) if values is None else values
    if plaintext:
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(data))
    else:
        save_tokens(str(path), data)
    return path


def preview(manager, service):
    return SourceImports(manager).preview(service, "Imported work", actor="user:owner")


def apply(manager, service, plan):
    return SourceImports(manager).apply(service, plan["plan_token"], actor="user:owner")


@pytest.mark.parametrize("service", ACCOUNT_READERS)
@pytest.mark.parametrize("plaintext", [False, True])
def test_account_import_is_secret_free_local_encrypted_and_idempotent(
    manager, service, plaintext
):
    legacy = seed(manager, service, plaintext=plaintext)
    before = legacy.read_bytes()
    plan = preview(manager, service)
    assert legacy.read_bytes() == before
    assert manager.store.list() == manager.credentials.list() == []
    assert not list_events(manager.store)
    assert "private-" not in json.dumps(plan)
    assert "fingerprint" not in plan and "legacy_binding" not in plan
    assert plan["credential_storage"] == "bundle"
    assert plan["oauth_grant_preserved"] == (ACCOUNT_READERS[service][3] == "oauth")
    record = apply(manager, service, plan)
    assert record == apply(manager, service, plan)
    assert record["adapter_id"] == f"{service}_account"
    assert "credential_id" not in record["config"]
    assert not record["legacy_document_ids"]
    assert len(manager.store.list()) == 1
    assert manager.credentials.list() == []  # Bundles are not scalar credentials.
    destination = token_path(manager.store.path.parent, record["id"])
    values = load_tokens(str(destination))
    assert values[
        "access_token"
        if ACCOUNT_READERS[service][3] == "oauth"
        else "api_key"
        if service == "weather"
        else "token"
    ].startswith(("private-", "xoxp-private-"))
    assert AccountSource(service, record, manager.store.path.parent).is_connected()
    if service == "weather":
        assert record["config"] == {"location": "Boston"}
        assert "location" not in values
    assert "private-" not in destination.read_text()
    assert b"private-" not in manager.credentials.path.read_bytes()
    assert b"private-" not in manager.store.path.read_bytes()
    assert load_tokens(str(legacy)) == payload(service)
    assert "private-" not in legacy.read_text()
    assert manager.list()[0]["chunks"] == 0
    assert len(list_events(manager.store)) == 1
    assert list_events(manager.store)[0]["action"] == "imported"
    assert "private-" not in json.dumps(list_events(manager.store))
    assert "private-" not in json.dumps(SourceImports(manager).list())


@pytest.mark.parametrize(
    "service", ["gdrive", "gcalendar", "gcontacts", "gmail", "google_tasks"]
)
def test_shared_google_fallback_preserves_existing_broad_grant_without_narrowing(
    manager, service
):
    values = payload(service)
    values["requested_scopes"] = GOOGLE_ALL_SCOPES
    seed(manager, service, values, filename="google.json")
    plan = preview(manager, service)
    assert plan["oauth_grant_preserved"] and plan["refresh_available"]
    record = apply(manager, service, plan)
    result = load_tokens(str(token_path(manager.store.path.parent, record["id"])))
    assert set(GOOGLE_ALL_SCOPES).issubset(result["requested_scopes"])
    assert (
        "https://www.googleapis.com/auth/userinfo.email" in result["requested_scopes"]
    )
    assert not (manager.store.path.parent / "connectors" / f"{service}.json").exists()


@pytest.mark.parametrize(
    "initial_filename,replacement",
    [
        ("google.json", "gdrive.json"),
        ("gdrive.json", "google.json"),
        ("github_notifications.json", "github.json"),
    ],
)
def test_credential_file_selection_changes_invalidate_preview(
    manager, initial_filename, replacement
):
    service = (
        "github_notifications" if initial_filename.startswith("github") else "gdrive"
    )
    initial = seed(manager, service, filename=initial_filename)
    plan = preview(manager, service)
    if initial_filename == "gdrive.json":
        delete_tokens(str(initial))
    seed(manager, service, filename=replacement)
    with pytest.raises(SourceConflict, match="selection changed"):
        apply(manager, service, plan)
    assert manager.store.list() == []


def test_present_invalid_product_file_does_not_borrow_shared_google_account(manager):
    seed(manager, "gdrive", filename="google.json")
    seed(manager, "gdrive", {"client_id": "test-application"})
    assert (
        next(
            item
            for item in SourceImports(manager).list()
            if item["import_id"] == "gdrive"
        )["state"]
        == "unavailable"
    )
    with pytest.raises(ValueError, match="unavailable"):
        preview(manager, "gdrive")


@pytest.mark.parametrize("service", ["gmail", "spotify", "strava"])
def test_access_only_legacy_tokens_import_without_claiming_refresh(manager, service):
    seed(
        manager,
        service,
        {"token" if service == "gmail" else "access_token": "private-access-only"},
    )
    plan = preview(manager, service)
    assert plan["oauth_grant_preserved"] and not plan["refresh_available"]
    record = apply(manager, service, plan)
    bundle = load_tokens(str(token_path(manager.store.path.parent, record["id"])))
    assert bundle["access_token"] == "private-access-only"
    assert "refresh_token" not in bundle


@pytest.mark.parametrize(
    "service,changes",
    [
        (
            "spotify",
            {
                "refresh_token": "private-refresh",
                "client_id": None,
                "client_secret": None,
            },
        ),
        (
            "gdrive",
            {"requested_scopes": ["https://www.googleapis.com/auth/gmail.readonly"]},
        ),
        ("gdrive", {"requested_scopes": "not-a-list"}),
        ("gdrive", {"scope": ["not-a-string"], "requested_scopes": None}),
        ("strava", {"expires_at": float("nan")}),
        ("gdrive", {"expires_at": 10**4000}),
        ("spotify", {"expires_in": -1}),
        ("spotify", {"client_secret": "invalid secret"}),
        ("slack", {"token": "xoxb-private-bot-token"}),
        ("weather", {"location": ""}),
        ("weather", {"location": "private-weather-api-key"}),
    ],
)
def test_unusable_credentials_never_appear_in_previews(manager, service, changes):
    data = {**payload(service), **changes}
    seed(manager, service, data)
    with pytest.raises(ValueError, match="unavailable") as error:
        preview(manager, service)
    assert "private-" not in str(error.value)
    assert (
        next(
            item
            for item in SourceImports(manager).list()
            if item["import_id"] == service
        )["state"]
        == "unavailable"
    )
    assert manager.store.list() == []


def test_untrusted_legacy_fields_are_not_copied_into_bundle_or_configuration(manager):
    seed(
        manager,
        "spotify",
        {
            **payload("spotify"),
            "token_endpoint": "https://evil.invalid",
            "redirect_uri": "https://evil.invalid",
            "unknown_secret": "private-extra",
        },
    )
    record = apply(manager, "spotify", preview(manager, "spotify"))
    bundle = load_tokens(str(token_path(manager.store.path.parent, record["id"])))
    assert (
        "token_endpoint" not in bundle
        and "redirect_uri" not in bundle
        and "unknown_secret" not in bundle
    )
    assert record["config"] == {}


@pytest.mark.parametrize("failure", ["before_source", "after_source", "marker"])
def test_restart_recovers_reserved_bundle_without_duplicates(
    manager, monkeypatch, failure
):
    seed(manager, "spotify")
    plan = preview(manager, "spotify")
    original = manager.store._insert_source
    if failure == "marker":
        original_marker = TokenVault._marker

        def marker(self):
            if self.path.name.startswith("instance-"):
                raise OSError("simulated marker failure")
            original_marker(self)

        monkeypatch.setattr(TokenVault, "_marker", marker)
    else:

        def insert(*args, **kwargs):
            if failure == "after_source":
                original(*args, **kwargs)
            raise OSError("simulated source commit failure")

        monkeypatch.setattr(manager.store, "_insert_source", insert)
    with pytest.raises(ValueError, match="failed"):
        apply(manager, "spotify", plan)
    assert manager.store.list() == [] and not list_events(manager.store)
    mapping = SourceImports(manager)._mapping("spotify")
    assert mapping and not mapping["completed"]
    destination = TokenVault(
        token_path(manager.store.path.parent, mapping["source_id"])
    )
    assert mapping["credential_id"] == destination.identity
    with destination.store._connection() as conn:
        assert conn.execute("SELECT count(*) FROM connector_tokens").fetchone()[0] == 2
    monkeypatch.setattr(manager.store, "_insert_source", original)
    if failure == "marker":
        monkeypatch.setattr(TokenVault, "_marker", original_marker)
    reopened = SourceManager(
        SourceStore(str(manager.store.path), legacy_path="missing"),
        knowledge_path=manager.knowledge_path,
    )
    record = apply(reopened, "spotify", plan)
    assert record["id"] == mapping["source_id"]
    assert load_tokens(str(destination.path))["access_token"] == "private-access-token"
    assert record == apply(reopened, "spotify", plan)
    with destination.store._connection() as conn:
        assert conn.execute("SELECT count(*) FROM connector_tokens").fetchone()[0] == 2


def test_recovery_refuses_changed_reserved_bundle(manager, monkeypatch):
    seed(manager, "spotify")
    plan = preview(manager, "spotify")
    original = manager.store._insert_source
    monkeypatch.setattr(
        manager.store,
        "_insert_source",
        lambda *a, **kw: (_ for _ in ()).throw(OSError()),
    )
    with pytest.raises(ValueError):
        apply(manager, "spotify", plan)
    mapping = SourceImports(manager)._mapping("spotify")
    destination = token_path(manager.store.path.parent, mapping["source_id"])
    save_tokens(str(destination), {"access_token": "independently-rotated"})
    monkeypatch.setattr(manager.store, "_insert_source", original)
    with pytest.raises(SourceConflict, match="credential changed"):
        apply(manager, "spotify", plan)
    assert load_tokens(str(destination))["access_token"] == "independently-rotated"
    assert manager.store.list() == []


def test_parallel_retries_one_bundle_and_source(manager):
    seed(manager, "spotify")
    plan = preview(manager, "spotify")

    def attempt():
        try:
            return apply(manager, "spotify", plan)["id"]
        except SourceConflict:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: attempt(), range(4)))
    assert len({result for result in results if result}) == 1
    assert len(manager.store.list()) == 1
    assert len(list_events(manager.store)) == 1


def test_completed_replay_keeps_named_changes_and_tombstone(manager):
    legacy = seed(manager, "spotify")
    plan = preview(manager, "spotify")
    record = apply(manager, "spotify", plan)
    destination = token_path(manager.store.path.parent, record["id"])
    save_tokens(str(destination), {"access_token": "named-rotation"})
    delete_tokens(str(legacy))
    assert apply(manager, "spotify", plan)["id"] == record["id"]
    assert load_tokens(str(destination))["access_token"] == "named-rotation"
    manager.delete(record["id"], record["revision"])
    assert load_tokens(str(destination)) is None
    with pytest.raises(SourceConflict, match="removed"):
        apply(manager, "spotify", plan)


def test_preview_binding_to_provider_actor_adapter_version_and_current_bundle(
    manager, monkeypatch
):
    legacy = seed(manager, "spotify")
    seed(manager, "strava")
    plan = preview(manager, "spotify")
    with pytest.raises(ValueError, match="invalid or expired"):
        SourceImports(manager).apply("strava", plan["plan_token"], actor="user:owner")
    with pytest.raises(ValueError, match="invalid or expired"):
        SourceImports(manager).apply("spotify", plan["plan_token"], actor="user:other")
    save_tokens(str(legacy), {**payload("spotify"), "access_token": "legacy-rotated"})
    with pytest.raises(SourceConflict, match="changed"):
        apply(manager, "spotify", plan)
    plan = preview(manager, "spotify")
    original = source_adapters.get_adapter("spotify_account")
    monkeypatch.setitem(
        source_adapters._ADAPTERS,
        "spotify_account",
        replace(original, config_version=2),
    )
    with pytest.raises(ValueError, match="invalid or expired"):
        apply(manager, "spotify", plan)
    assert manager.store.list() == []


def test_recovery_refuses_malformed_reserved_marker(manager, monkeypatch):
    seed(manager, "spotify")
    plan = preview(manager, "spotify")
    original = manager.store._insert_source
    monkeypatch.setattr(
        manager.store,
        "_insert_source",
        lambda *a, **kw: (_ for _ in ()).throw(OSError()),
    )
    with pytest.raises(ValueError):
        apply(manager, "spotify", plan)
    mapping = SourceImports(manager)._mapping("spotify")
    destination = token_path(manager.store.path.parent, mapping["source_id"])
    destination.write_text("independently changed malformed credential file")
    monkeypatch.setattr(manager.store, "_insert_source", original)
    with pytest.raises(SourceConflict, match="credential changed"):
        apply(manager, "spotify", plan)
    assert destination.read_text() == "independently changed malformed credential file"
    assert manager.store.list() == []
