"""Shared fixtures for server route tests.

Server tests build apps via ``create_app``, which (with traces enabled by
default) wires a ``TraceStore`` at the real ``~/.openjarvis/traces.db``. Now
that the chat endpoints actually *write* traces, an unguarded run would
pollute the developer's real trace DB and make tests non-hermetic. This
autouse fixture redirects the traces DB to a per-test temp path.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_runtime_tools(tmp_path, monkeypatch):
    from openjarvis.tools import runtime_manager

    real_store = runtime_manager.RuntimeToolStore

    def isolated_store(db_path):
        # Keep explicit temporary stores, redirect the default home database.
        from pathlib import Path

        from openjarvis.core.config import get_config_dir

        if Path(db_path).expanduser() == get_config_dir() / "runtime_tools.db":
            db_path = tmp_path / "runtime_tools.db"
        return real_store(db_path)

    monkeypatch.setattr(runtime_manager, "RuntimeToolStore", isolated_store)


@pytest.fixture(autouse=True)
def _isolate_auth_db(tmp_path, monkeypatch):
    """Give each test its own real auth store without changing auth behavior."""
    from openjarvis.server import auth_store

    real_auth_store = auth_store.AuthStore

    class IsolatedAuthStore(real_auth_store):
        def __init__(self, db_path=None):
            super().__init__(db_path if db_path is not None else tmp_path / "auth.db")

    monkeypatch.setattr(auth_store, "AuthStore", IsolatedAuthStore)


@pytest.fixture(autouse=True)
def _isolate_traces_db(tmp_path, monkeypatch):
    """Point ``config.traces.db_path`` at a temp file for every server test.

    ``load_config`` returns a fresh ``JarvisConfig`` per call (no caching), so
    wrapping it to rewrite ``traces.db_path`` only affects calls made during
    the test — there is no global leak.
    """
    from openjarvis.core import config as _config

    real_load_config = _config.load_config
    db_path = str(tmp_path / "traces.db")

    def _patched_load_config(*args, **kwargs):
        cfg = real_load_config(*args, **kwargs)
        cfg.traces.db_path = db_path
        return cfg

    monkeypatch.setattr(_config, "load_config", _patched_load_config)
    return db_path
