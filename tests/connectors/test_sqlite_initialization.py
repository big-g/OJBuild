"""Real database startup contention, with no provider traffic."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from openjarvis.connectors._sqlite import initialize_wal
from openjarvis.connectors.pipeline import IngestionPipeline
from openjarvis.connectors.store import KnowledgeStore
from openjarvis.connectors.sync_engine import SyncEngine


def test_wal_initialization_waits_for_existing_reader(tmp_path):
    path = tmp_path / "database.db"
    with sqlite3.connect(path) as setup:
        setup.execute("CREATE TABLE entries (value TEXT)")
        setup.execute("INSERT INTO entries VALUES ('evidence')")
    blocker = sqlite3.connect(path)
    blocker.execute("BEGIN")
    blocker.execute("SELECT * FROM entries").fetchall()
    attempted, completed = threading.Event(), threading.Event()

    def initialize():
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA busy_timeout=0")
            attempted.set()
            initialize_wal(connection)
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 0
            completed.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(initialize)
        try:
            assert attempted.wait(1)
            assert not completed.wait(0.1)
        finally:
            blocker.rollback()
            blocker.close()
        future.result(timeout=2)


def test_concurrent_knowledge_and_checkpoint_startup(tmp_path):
    knowledge = tmp_path / "knowledge.db"
    state = tmp_path / "state.db"
    barrier = threading.Barrier(4)

    def initialize():
        barrier.wait(timeout=2)
        with KnowledgeStore(knowledge) as store:
            with SyncEngine(IngestionPipeline(store), state_db=str(state)) as engine:
                assert store.count() == 0
                assert engine.get_checkpoint("new-source") is None

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(initialize) for _ in range(4)]
        for future in futures:
            future.result(timeout=5)
