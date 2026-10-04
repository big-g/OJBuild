"""Deleting test accounts revokes credentials without affecting other accounts."""

import pytest
from click.testing import CliRunner

from openjarvis.cli.auth_cmd import auth
from openjarvis.server.auth_store import AuthStore


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    store = AuthStore(tmp_path / "auth.db")
    store.create_user("test-id", "test-user", "password")
    monkeypatch.setattr("openjarvis.cli.auth_cmd.AuthStore", lambda: store)
    return store


def test_delete_revokes_credentials_and_allows_fresh_identity(accounts):
    accounts.create_user("other-id", "other", "password")
    token = accounts.create_session("test-id")
    code = accounts.issue_recovery_code("test-id")["recovery_code"]
    other_token = accounts.create_session("other-id")
    result = CliRunner().invoke(
        auth, ["delete-user", "--username", "test-user"], input="y\n"
    )
    assert result.exit_code == 0, result.output
    assert accounts.get_user("test-id") is None
    assert accounts.authenticate("test-user", "password") is None
    assert accounts.get_user_for_token(token) is None
    with pytest.raises(ValueError, match="Invalid or expired"):
        accounts.recover_account(code)
    assert accounts.get_user_for_token(other_token)["user_id"] == "other-id"
    with accounts._connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM auth_sessions WHERE user_id = 'test-id'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM account_recovery WHERE user_id = 'test-id'"
        ).fetchone()[0] == 0
    accounts.create_user("fresh-id", "test-user", "new-password")
    assert accounts.authenticate("test-user", "new-password")["user_id"] == "fresh-id"


@pytest.mark.parametrize("answer", ["n\n", "\n"])
def test_cancel_preserves_account_and_credentials(accounts, answer):
    token = accounts.create_session("test-id")
    code = accounts.issue_recovery_code("test-id")["recovery_code"]
    result = CliRunner().invoke(
        auth, ["delete-user", "--username", "test-user"], input=answer
    )
    assert result.exit_code != 0
    assert accounts.get_user_for_token(token) is not None
    assert accounts.recover_account(code) == "test-user"


def test_yes_can_remove_last_account_and_unknown_is_error(accounts):
    runner = CliRunner()
    result = runner.invoke(auth, ["delete-user", "--username", "test-user", "--yes"])
    assert result.exit_code == 0
    assert "last account" in result.output
    assert accounts.list_users() == []
    result = runner.invoke(auth, ["delete-user", "--username", "missing", "--yes"])
    assert result.exit_code != 0
    with pytest.raises(ValueError, match="Account not found"):
        accounts.delete_user("missing")
