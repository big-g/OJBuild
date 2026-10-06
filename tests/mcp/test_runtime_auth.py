"""API keys remain encrypted and bound to the reviewed authentication contract."""

import json

import pytest

from openjarvis.mcp.runtime_auth import authentication
from openjarvis.mcp.runtime_manager import RuntimeMCPManager
from openjarvis.mcp.runtime_store import canonical, definition
from tests.mcp.test_runtime_connections import call, config
from tests.mcp.test_runtime_connections import remote as remote


def api_config(**changes):
    return config(auth_type="api_key", api_key_header="X-API-Key") | changes


def test_api_key_lifecycle_rotation_header_edits_and_restart(tmp_path, remote):
    path = tmp_path / "mcp.db"
    manager = RuntimeMCPManager(path)
    row = manager.store.create(api_config(), "admin", "TEST-SECRET")
    assert b"TEST-SECRET" not in path.read_bytes()
    assert "TEST-SECRET" not in json.dumps(manager.view(row))
    assert not remote["clients"]
    row = manager.discover(row["id"], 1, "admin")
    row = manager.change(row["id"], row["revision"], "approved", "admin")
    restarted = RuntimeMCPManager(path)
    tool = restarted.available()[0]
    assert call(tool).success
    assert remote["clients"][-1].auth["api_key_header"] == "x-api-key"
    row = manager.change(
        row["id"],
        row["revision"],
        "updated",
        "admin",
        config=api_config(api_key_header="x-api-key"),
    )
    assert manager.store.token(row) == "TEST-SECRET"  # Header case is immaterial.
    assert not call(tool).success
    row = manager.change(
        row["id"],
        row["revision"],
        "updated",
        "admin",
        config=api_config(api_key_header="X-Service-Key"),
    )
    assert manager.store.token(row) == "" and not row["discovered"]
    with pytest.raises(ValueError, match="discovery failed"):
        manager.discover(row["id"], row["revision"], "admin")
    row = manager.store.get(row["id"])
    row = manager.change(
        row["id"],
        row["revision"],
        "updated",
        "admin",
        config=api_config(api_key_header="X-Service-Key"),
        token="ROTATED-SECRET",
    )
    row = manager.discover(row["id"], row["revision"], "admin")
    row = manager.change(row["id"], row["revision"], "approved", "admin")
    assert call(restarted.available()[0]).success
    assert remote["clients"][-1].token == "ROTATED-SECRET"
    row = manager.change(
        row["id"], row["revision"], "updated", "admin", config=config()
    )
    assert manager.store.token(row) == ""
    assert "ROTATED-SECRET" not in json.dumps(manager.store.audit(row["id"]))


def test_old_bearer_ciphertext_defaults_and_binding_cannot_be_repurposed(tmp_path):
    manager = RuntimeMCPManager(tmp_path / "mcp.db")
    row = manager.store.create(config(), "admin", "TEST-SECRET")
    assert definition(config(auth_type="bearer", api_key_header="")) == definition(
        config()
    )
    # Simulate a published v1 vault payload without authentication metadata.
    sealed = manager.store.vault._cipher().encrypt(
        canonical(
            {"id": row["id"], "url": config()["url"], "token": "TEST-SECRET"}
        ).encode()
    )
    with manager.store.vault._connection() as db:
        db.execute(
            "UPDATE connector_tokens SET sealed=? WHERE id=?", (sealed, row["id"])
        )
    assert manager.store.token(row) == "TEST-SECRET"
    with manager.store.vault._connection() as db:
        db.execute(
            "UPDATE runtime_mcp SET definition=? WHERE id=?",
            (canonical(definition(api_config())), row["id"]),
        )
    with pytest.raises(ValueError, match="could not be unlocked"):
        manager.store.token(manager.store.get(row["id"]))


def test_api_key_ciphertext_cannot_be_rebound_and_wrong_key_blocks_rotation(tmp_path):
    manager = RuntimeMCPManager(tmp_path / "mcp.db")
    row = manager.store.create(api_config(), "admin", "TEST-SECRET")
    original = row["definition"]
    with manager.store.vault._connection() as db:
        db.execute(
            "UPDATE runtime_mcp SET definition=? WHERE id=?",
            (
                canonical(definition(api_config(api_key_header="X-Other-Key"))),
                row["id"],
            ),
        )
    with pytest.raises(ValueError, match="could not be unlocked"):
        manager.store.token(manager.store.get(row["id"]))
    with manager.store.vault._connection() as db:
        db.execute(
            "UPDATE runtime_mcp SET definition=? WHERE id=?", (original, row["id"])
        )
    manager.store.vault.key_path.unlink()
    with pytest.raises(ValueError):
        manager.store.change(
            row["id"], 1, "updated", "admin", config=api_config(), token="REPLACEMENT"
        )
    assert not manager.store.vault.key_path.exists()


@pytest.mark.parametrize(
    "settings",
    [
        {"auth_type": []},
        {"auth_type": "oauth"},
        {"auth_type": "api_key"},
        {"auth_type": "bearer", "api_key_header": "X-Key"},
        {"auth_type": "api_key", "api_key_header": None},
    ],
)
def test_invalid_authentication_settings_are_rejected(settings):
    with pytest.raises(ValueError):
        authentication(settings)
