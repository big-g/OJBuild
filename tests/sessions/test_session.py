"""Tests for session management (Phase 15.4)."""

from __future__ import annotations

import time

from openjarvis.sessions.session import (
    Session,
    SessionIdentity,
    SessionStore,
)


class TestSession:
    def test_create_session(self):
        session = Session(session_id="s1")
        assert session.session_id == "s1"
        assert len(session.messages) == 0

    def test_add_message(self):
        session = Session(session_id="s1")
        session.add_message("user", "Hello")
        assert len(session.messages) == 1
        assert session.messages[0].role == "user"
        assert session.messages[0].content == "Hello"
        assert session.last_activity > 0


class TestSessionIdentity:
    def test_create_identity(self):
        identity = SessionIdentity(
            user_id="u1",
            display_name="Alice",
            channel_ids={"telegram": "t123"},
        )
        assert identity.user_id == "u1"
        assert identity.channel_ids["telegram"] == "t123"


class TestSessionStore:
    def _make_store(self, tmp_path, **kwargs):
        return SessionStore(db_path=tmp_path / "sessions.db", **kwargs)

    def test_create_session(self, tmp_path):
        store = self._make_store(tmp_path)
        session = store.get_or_create("user1", display_name="Alice")
        assert session.session_id != ""
        assert session.identity is not None
        assert session.identity.user_id == "user1"
        store.close()

    def test_get_existing_session(self, tmp_path):
        store = self._make_store(tmp_path)
        s1 = store.get_or_create("user1")
        s2 = store.get_or_create("user1")
        assert s1.session_id == s2.session_id
        store.close()

    def test_save_message(self, tmp_path):
        store = self._make_store(tmp_path)
        session = store.get_or_create("user1")
        store.save_message(session.session_id, "user", "Hello")
        store.save_message(session.session_id, "assistant", "Hi there!")

        # Reload session
        reloaded = store.get_or_create("user1")
        assert len(reloaded.messages) == 2
        assert reloaded.messages[0].content == "Hello"
        assert reloaded.messages[1].content == "Hi there!"
        store.close()

    def test_first_user_message_sets_shared_session_title(self, tmp_path):
        store = self._make_store(tmp_path)
        session = store.get_or_create("user1")
        store.save_message(session.session_id, "user", "What is the weather?")
        store.save_message(session.session_id, "user", "And tomorrow?")

        reloaded = store.get_session(session.session_id)
        assert reloaded is not None
        assert reloaded.title == "What is the weather?"
        store.close()

    def test_delete_session_removes_its_messages(self, tmp_path):
        store = self._make_store(tmp_path)
        session = store.get_or_create("user1")
        store.save_message(session.session_id, "user", "Hello")

        assert store.delete_session(session.session_id) is True
        assert store.get_session(session.session_id) is None
        assert store.delete_session(session.session_id) is False
        store.close()

    def test_replace_messages_preserves_imported_metadata(self, tmp_path):
        store = self._make_store(tmp_path)
        session = store.get_or_create("user1")
        imported = [
            {
                "role": "assistant",
                "content": "Research result",
                "timestamp": 123.5,
                "metadata": {"researchSources": [{"ref": 1}]},
            }
        ]

        assert store.replace_messages(session.session_id, imported) is True
        reloaded = store.get_session(session.session_id)
        assert reloaded is not None
        assert reloaded.messages[0].timestamp == 123.5
        assert reloaded.messages[0].metadata == {"researchSources": [{"ref": 1}]}
        store.close()

    def test_link_channel(self, tmp_path):
        store = self._make_store(tmp_path)
        session = store.get_or_create("user1")
        store.link_channel(session.session_id, "telegram", "t123")
        store.link_channel(session.session_id, "discord", "d456")

        reloaded = store.get_or_create("user1")
        assert reloaded.identity.channel_ids.get("telegram") == "t123"
        assert reloaded.identity.channel_ids.get("discord") == "d456"
        store.close()

    def test_session_persists_across_inactivity(self, tmp_path):
        store = self._make_store(tmp_path, max_age_hours=0.0001)
        s1 = store.get_or_create("user1")
        time.sleep(0.5)
        s2 = store.get_or_create("user1")
        assert s1.session_id == s2.session_id
        store.close()

    def test_decay(self, tmp_path):
        store = self._make_store(tmp_path, max_age_hours=0.0001)
        store.get_or_create("user1")
        time.sleep(0.5)
        removed = store.decay()
        assert removed >= 1
        store.close()

    def test_list_sessions(self, tmp_path):
        store = self._make_store(tmp_path)
        store.get_or_create("user1")
        store.get_or_create("user2")
        sessions = store.list_sessions()
        assert len(sessions) == 2
        store.close()

    def test_consolidation(self, tmp_path):
        store = self._make_store(tmp_path, consolidation_threshold=5)
        session = store.get_or_create("user1")
        for i in range(10):
            store.save_message(session.session_id, "user", f"msg {i}")
        # After saving 10 messages with threshold=5, consolidation should trigger
        reloaded = store.get_or_create("user1")
        # Messages should be fewer after consolidation
        assert len(reloaded.messages) < 10
        store.close()

    def test_cross_channel_session(self, tmp_path):
        store = self._make_store(tmp_path)
        s1 = store.get_or_create("user1", channel="telegram", channel_user_id="t1")
        store.save_message(s1.session_id, "user", "From Telegram", channel="telegram")
        store.link_channel(s1.session_id, "discord", "d1")
        store.save_message(s1.session_id, "user", "From Discord", channel="discord")

        reloaded = store.get_or_create("user1")
        assert len(reloaded.messages) == 2
        assert reloaded.messages[0].channel == "telegram"
        assert reloaded.messages[1].channel == "discord"
        store.close()
