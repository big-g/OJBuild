"""Local bootstrap selects roles without automatically promoting accounts."""

from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from openjarvis.cli.auth_cmd import auth
from openjarvis.server.auth_store import AuthStore


@pytest.mark.parametrize("role", [None, "user", "administrator"])
def test_create_user_selects_explicit_role(tmp_path, monkeypatch, role):
    store = AuthStore(tmp_path / "auth.db")
    monkeypatch.setattr("openjarvis.cli.auth_cmd.AuthStore", lambda: store)
    monkeypatch.setattr("openjarvis.cli.auth_cmd.SessionStore", MagicMock())
    args = ["create-user", "--username", "new-user", "--display-name", "New User"]
    if role:
        args += ["--role", role]
    result = CliRunner().invoke(auth, args, input="password123\npassword123\n")
    assert result.exit_code == 0, result.output
    assert bool(store.get_user_by_username("new-user")["is_admin"]) == (
        role == "administrator"
    )
    assert f"Role:         {role or 'user'}" in result.output
    assert "password123" not in result.output


def test_local_bootstrap_can_designate_existing_admin(tmp_path, monkeypatch):
    store = AuthStore(tmp_path / "auth.db")
    store.create_user("existing", "existing", "password123")
    monkeypatch.setattr("openjarvis.cli.auth_cmd.AuthStore", lambda: store)
    result = CliRunner().invoke(auth, ["set-admin", "--username", "existing"])
    assert result.exit_code == 0, result.output
    assert store.get_user("existing")["is_admin"]
