"""Local recovery exposes account names, preserves identities and revokes logins."""

import json

from click.testing import CliRunner

from openjarvis.cli.auth_cmd import auth
from openjarvis.server.auth_store import AuthStore
from openjarvis.sessions.session import SessionStore


def test_list_users_excludes_password_hashes_and_tokens(tmp_path, monkeypatch):
    store = AuthStore(tmp_path / "auth.db")
    store.create_user("stable-id", "gary", "old-password", display_name="Gary")
    token = store.create_session("stable-id")
    monkeypatch.setattr("openjarvis.cli.auth_cmd.AuthStore", lambda: store)
    result = CliRunner().invoke(auth, ["list-users", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == [
        {
            "user_id": "stable-id",
            "username": "gary",
            "display_name": "Gary",
            "disabled": 0,
            "is_admin": 0,
        }
    ]
    assert "password" not in result.output
    assert token not in result.output
    assert store.get_user("stable-id")["password_hash"] not in result.output
    result = CliRunner().invoke(auth, ["list-users"])
    assert "gary\tGary\tactive" in result.output


def test_reset_password_preserves_conversations_and_revokes_only_target(
    tmp_path, monkeypatch
):
    store = AuthStore(tmp_path / "auth.db")
    store.create_user("stable-id", "gary", "old-password")
    store.create_user("other", "other", "other-password")
    tokens = [store.create_session("stable-id") for _ in range(2)]
    other_token = store.create_session("other")
    sessions = SessionStore(tmp_path / "sessions.db")
    project = sessions.create_project("stable-id", "Existing")
    session = sessions.create_session("stable-id", project.project_id)
    sessions.save_message(session.session_id, "user", "Keep this conversation")
    monkeypatch.setattr("openjarvis.cli.auth_cmd.AuthStore", lambda: store)
    result = CliRunner().invoke(
        auth,
        ["reset-password", "--username", "gary"],
        input="new-password\nnew-password\n",
    )
    assert result.exit_code == 0, result.output
    assert "new-password" not in result.output
    assert store.authenticate("gary", "old-password") is None
    assert store.authenticate("gary", "new-password")["user_id"] == "stable-id"
    assert all(store.get_user_for_token(token) is None for token in tokens)
    assert store.get_user_for_token(other_token) is not None
    saved = sessions.get_session(session.session_id)
    assert saved.identity.user_id == "stable-id"
    assert saved.messages[0].content == "Keep this conversation"
    sessions.close()


def test_empty_store_and_unknown_reset_do_not_create_accounts(tmp_path, monkeypatch):
    store = AuthStore(tmp_path / "auth.db")
    monkeypatch.setattr("openjarvis.cli.auth_cmd.AuthStore", lambda: store)
    runner = CliRunner()
    assert "No local OpenJarvis accounts" in runner.invoke(auth, ["list-users"]).output
    result = runner.invoke(auth, ["reset-password", "--username", "missing"])
    assert result.exit_code != 0
    assert store.list_users() == []


def test_administrator_designation_is_explicit_and_revocable(tmp_path, monkeypatch):
    store = AuthStore(tmp_path / "auth.db")
    store.create_user("owner", "gary", "password")
    monkeypatch.setattr("openjarvis.cli.auth_cmd.AuthStore", lambda: store)
    runner = CliRunner()
    assert store.get_user("owner")["is_admin"] == 0
    assert runner.invoke(auth, ["set-admin", "--username", "gary"]).exit_code == 0
    assert store.get_user("owner")["is_admin"] == 1
    assert (
        runner.invoke(auth, ["set-admin", "--username", "gary", "--revoke"]).exit_code
        == 0
    )
    assert store.get_user("owner")["is_admin"] == 0
