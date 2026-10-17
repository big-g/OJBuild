"""Maintenance resets use actual SQLite/FTS stores, not deletion mocks."""

import importlib.util
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from openjarvis.artifacts.store import ArtifactStore
from openjarvis.core.config import JarvisConfig
from openjarvis.server.auth_store import AuthStore
from openjarvis.server.content_protection import ContentProtection
from openjarvis.server.session_store import SessionStore
from openjarvis.sessions.session import SessionStore as ProjectSessions
from openjarvis.workflow.jobs import JobsStore

spec = importlib.util.spec_from_file_location(
    "reset_memory", Path(__file__).parents[1] / "scripts" / "reset_memory.py"
)
resetter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resetter)


def read(path, sql):
    with sqlite3.connect(path) as db:
        return db.execute(sql).fetchall()


@pytest.fixture
def configured(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config = JarvisConfig()
    config.memory.db_path = str(tmp_path / "relocated-memory.db")
    config.memory.facts_path = str(tmp_path / "relocated-facts.jsonl")
    config.sessions.db_path = str(home / "sessions.db")
    config.agent_manager.db_path = str(home / "agents.db")
    config.security.generated_files_dir = str(home / "artifacts")
    config.memory_files.memory_path = str(home / "MEMORY.md")
    config.memory_files.user_path = str(home / "USER.md")
    config.memory_files.soul_path = str(home / "SOUL.md")
    for path in [
        config.memory.facts_path,
        config.memory_files.memory_path,
        config.memory_files.user_path,
        config.memory_files.soul_path,
    ]:
        Path(path).write_text("remember this")
    with sqlite3.connect(config.memory.db_path) as db:
        db.executescript("""CREATE TABLE documents(id TEXT, content TEXT);
            CREATE VIRTUAL TABLE documents_fts USING fts5(content);
            INSERT INTO documents VALUES ('1','test memory');
            INSERT INTO documents_fts VALUES ('test memory');""")
    return config, home


def test_reset_backup_restore_and_preserve_accounts(configured):
    config, home = configured
    auth = AuthStore(home / "auth.db")
    ContentProtection(auth)
    sessions = SessionStore(str(home / "sessions.db"))
    projects = ProjectSessions(home / "sessions.db")
    with sqlite3.connect(home / "sessions.db") as db:
        db.execute(
            "INSERT INTO channel_sessions(sender_id,channel_type) VALUES('u','web')"
        )
    before_auth = read(home / "auth.db", "SELECT * FROM users")
    databases, texts = resetter.plan(config, home)
    backup = home / "backup"
    resetter.reset(databases, texts, backup)
    assert read(Path(config.memory.db_path), "SELECT * FROM documents") == []
    assert read(Path(config.memory.db_path), "SELECT * FROM documents_fts") == []
    assert read(home / "sessions.db", "SELECT * FROM channel_sessions") == []
    assert Path(config.memory.facts_path).read_text() == ""
    assert (home / "MEMORY.md").read_text() == ""
    assert (home / "USER.md").read_text() == "remember this"
    assert (home / "SOUL.md").read_text() == "remember this"
    assert read(home / "auth.db", "SELECT * FROM users") == before_auth
    assert backup.stat().st_mode & 0o777 == 0o700
    resetter.restore(backup)
    assert read(Path(config.memory.db_path), "SELECT content FROM documents") == [
        ("test memory",)
    ]
    assert Path(config.memory.facts_path).read_text() == "remember this"
    sessions._db.close()
    projects._conn.close()


def test_only_generated_outputs_deleted_and_restorable(configured):
    config, home = configured
    store = ArtifactStore(config.security.generated_files_dir)
    uploaded = store.save("test-user", "upload.png", "upload")
    generated = store.save("test-user", "generated.png", "output")
    jobs = JobsStore(store.root / "workflows" / "jobs.db")
    result = {
        "steps": [
            {
                "input": {"artifact_id": uploaded["id"]},
                "output": {"artifact": generated},
            },
            {
                "input": {"artifact_id": uploaded["id"]},
                "output": {"artifact": uploaded},
            },  # inspection carries input through
        ]
    }
    with sqlite3.connect(jobs.path) as db:
        db.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                "test-user",
                None,
                1,
                "{}",
                "{}",
                "completed",
                json.dumps(result),
                1,
                0,
            ),
        )
        db.execute("INSERT INTO definitions VALUES('saved','test-user',1,'{}')")
    databases, texts = resetter.plan(config, home)
    deletions = resetter.generated_plan(config, home, databases)
    backup = home / "backup"
    resetter.reset(databases, texts, backup, deletions)
    assert store.read("test-user", uploaded["id"])[1] == b"upload"
    with pytest.raises(ValueError):
        store.read("test-user", generated["id"])
    assert read(jobs.path, "SELECT * FROM runs") == []
    assert read(jobs.path, "SELECT id FROM definitions") == [("saved",)]
    resetter.restore(backup)
    assert store.read("test-user", generated["id"])[1] == b"output"


def test_all_files_removes_uploaded_artifacts(configured):
    config, home = configured
    store = ArtifactStore(config.security.generated_files_dir)
    upload = store.save("user", "upload.png", "upload")
    databases, texts = resetter.plan(config, home)
    files = resetter.generated_plan(config, home, databases, all_files=True)
    resetter.reset(databases, texts, home / "backup", files)
    with pytest.raises(ValueError):
        store.read("user", upload["id"])


def test_reset_failure_rolls_back_every_store(configured):
    config, home = configured
    databases, texts = resetter.plan(config, home)
    databases[Path(config.memory.db_path)]["documents"] = "DELETE FROM missing_table"
    with pytest.raises(sqlite3.OperationalError):
        resetter.reset(databases, texts, home / "backup")
    assert read(Path(config.memory.db_path), "SELECT content FROM documents") == [
        ("test memory",)
    ]
    assert Path(config.memory.facts_path).read_text() == "remember this"


def test_path_collision_refused(configured):
    config, home = configured
    config.memory.facts_path = config.memory.db_path
    with pytest.raises(ValueError, match="overlaps"):
        resetter.plan(config, home)


def test_keep_conversations_and_include_profile(configured):
    config, home = configured
    databases, texts = resetter.plan(
        config, home, keep_conversations=True, include_profile=True
    )
    assert home / "sessions.db" not in databases
    assert home / "agents.db" not in databases
    assert home / "USER.md" in texts


def test_worker_output_scan_rejects_model_weights_and_symlinks(tmp_path):
    output = tmp_path / "outputs"
    output.mkdir()
    (output / "mesh.glb").write_bytes(b"glb")
    assert resetter.worker_files([output]) == [output / "mesh.glb"]
    (output / "weights.safetensors").write_bytes(b"model")
    with pytest.raises(ValueError, match="Unrecognized"):
        resetter.worker_files([output])
    (output / "weights.safetensors").unlink()
    (output / "link").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="Symlink"):
        resetter.worker_files([output])


def test_sqlite_backup_includes_wal(configured):
    config, home = configured
    connection = sqlite3.connect(config.memory.db_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("INSERT INTO documents VALUES ('2','WAL memory')")
    connection.commit()
    databases, texts = resetter.plan(config, home)
    resetter.reset(databases, texts, home / "backup")
    resetter.restore(home / "backup")
    assert ("WAL memory",) in read(
        Path(config.memory.db_path), "SELECT content FROM documents"
    )
    connection.close()


def setup_cli(monkeypatch, configured, arguments):
    from unittest.mock import Mock

    config, home = configured
    monkeypatch.setattr(resetter.os, "geteuid", lambda: 1000)
    monkeypatch.setattr("openjarvis.core.config.load_config", Mock(return_value=config))
    monkeypatch.setattr("openjarvis.core.paths.get_config_dir", lambda: home)
    monkeypatch.setattr(resetter.sys, "argv", ["reset_memory.py", *arguments])


def test_preview_does_not_stop_services_or_mutate(monkeypatch, configured):
    from unittest.mock import Mock

    setup_cli(monkeypatch, configured, [])
    service = Mock(side_effect=AssertionError("Preview must not control services"))
    monkeypatch.setattr(resetter, "service_command", service)
    config, home = configured
    resetter.main()
    assert Path(config.memory.facts_path).read_text() == "remember this"
    assert not (home / "backups").exists()
    service.assert_not_called()


def test_service_stopped_before_reset_restarted_after(monkeypatch, configured):
    setup_cli(monkeypatch, configured, ["--apply"])
    events = []

    def service(user, action, unit):
        events.append(action)
        if action == "is-active":
            return "inactive" if "stop" in events else "active"
        return ""

    monkeypatch.setattr(resetter, "service_command", service)
    original = resetter.reset

    def resetting(*args):
        assert events == ["is-active", "stop", "is-active"]
        events.append("reset")
        return original(*args)

    monkeypatch.setattr(resetter, "reset", resetting)
    resetter.main()
    assert events[-2:] == ["reset", "start"]


def test_stop_failure_prevents_reset(monkeypatch, configured):
    setup_cli(monkeypatch, configured, ["--apply"])
    monkeypatch.setattr(resetter, "service_command", lambda *args: "active")
    with pytest.raises(RuntimeError, match="did not stop"):
        resetter.main()
    assert Path(configured[0].memory.facts_path).read_text() == "remember this"


def test_managed_agents_config_retained_memory_cleared(configured):
    from openjarvis.agents.manager import AgentManager

    config, home = configured
    manager = AgentManager(str(home / "agents.db"))
    with sqlite3.connect(home / "agents.db") as db:
        db.execute(
            "INSERT INTO managed_agents "
            "(id,name,agent_type,config_json,status,summary_memory,"
            "created_at,updated_at) "
            "VALUES('a','agent','test', '{}', 'idle', 'learned', 1, 1)"
        )
    databases, texts = resetter.plan(config, home)
    resetter.reset(databases, texts, home / "backup")
    assert read(
        home / "agents.db", "SELECT name,summary_memory FROM managed_agents"
    ) == [("agent", "")]
    manager._conn.close()


def test_knowledge_reset_keeps_external_fts_consistent(configured):
    from openjarvis.connectors.store import KnowledgeStore

    config, home = configured
    store = KnowledgeStore(str(home / "knowledge.db"))
    with sqlite3.connect(home / "knowledge.db") as db:
        db.execute(
            "INSERT INTO knowledge_chunks(id,doc_id,content,created_at) "
            "VALUES('x','d','remember',1)"
        )
    databases, texts = resetter.plan(config, home, include_knowledge=True)
    resetter.reset(databases, texts, home / "backup")
    assert read(home / "knowledge.db", "SELECT * FROM knowledge_chunks") == []
    assert (
        read(
            home / "knowledge.db",
            "SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH 'remember'",
        )
        == []
    )
    store.close()
