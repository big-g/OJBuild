"""Session management — cross-channel persistent sessions.

Supports consolidation and decay.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from openjarvis.core.config import DEFAULT_CONFIG_DIR


@dataclass(slots=True)
class SessionIdentity:
    """Canonical user identity across channels."""

    user_id: str
    display_name: str = ""
    # channel_type -> channel_user_id
    channel_ids: Dict[str, str] = field(
        default_factory=dict,
    )


@dataclass(slots=True)
class SessionMessage:
    """A single message within a session."""

    role: str  # "user" | "assistant" | "system"
    content: str
    channel: str = ""
    timestamp: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Project:
    """A persistent workspace belonging to a user."""

    project_id: str = ""
    user_id: str = ""
    name: str = ""
    description: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Session:
    """A conversation session with cross-channel message history."""

    session_id: str = ""
    identity: Optional[SessionIdentity] = None
    project_id: str = ""
    project_name: str = ""
    title: str = ""
    messages: List[SessionMessage] = field(default_factory=list)
    created_at: float = 0.0
    last_activity: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def add_message(self, role: str, content: str, *, channel: str = "") -> None:
        self.messages.append(
            SessionMessage(
                role=role,
                content=content,
                channel=channel,
                timestamp=time.time(),
            )
        )
        self.last_activity = time.time()


class SessionStore:
    """SQLite-backed session persistence with consolidation and decay."""

    def __init__(
        self,
        db_path: Union[str, Path] = DEFAULT_CONFIG_DIR / "sessions.db",
        *,
        max_age_hours: float = 24.0,
        consolidation_threshold: int = 100,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._max_age_hours = max_age_hours
        self._consolidation_threshold = consolidation_threshold
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id       TEXT PRIMARY KEY,
                display_name  TEXT DEFAULT '',
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL,
                metadata      TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS projects (
                project_id    TEXT PRIMARY KEY,
                user_id       TEXT NOT NULL,
                name          TEXT NOT NULL,
                description   TEXT DEFAULT '',
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL,
                metadata      TEXT DEFAULT '{}',
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_projects_user
                ON projects(user_id);

            CREATE TABLE IF NOT EXISTS sessions (
                session_id    TEXT PRIMARY KEY,
                user_id       TEXT,
                project_id    TEXT,
                display_name  TEXT DEFAULT '',
                title         TEXT DEFAULT '',
                channel_ids   TEXT DEFAULT '{}',
                created_at    REAL,
                last_activity REAL,
                metadata      TEXT DEFAULT '{}',
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (project_id) REFERENCES projects(project_id)
            );
            CREATE TABLE IF NOT EXISTS session_messages (
                id         INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                channel    TEXT DEFAULT '',
                timestamp  REAL,
                metadata   TEXT DEFAULT '{}',
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON session_messages(session_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_user
                ON sessions(user_id);
        """)
        # Migrate databases created by older OpenJarvis versions.
        for column, definition in (
            ("project_id", "TEXT"),
            ("title", "TEXT DEFAULT ''"),
        ):
            try:
                self._conn.execute(
                    f"ALTER TABLE sessions ADD COLUMN {column} {definition}"
                )
            except sqlite3.OperationalError:
                pass

        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id)"
        )

        self._conn.commit()

    def ensure_user(
        self,
        user_id: str,
        display_name: str = "",
    ) -> None:
        """Create or update a user identity."""
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO users (user_id, display_name, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                display_name = CASE
                    WHEN excluded.display_name != ''
                    THEN excluded.display_name
                    ELSE users.display_name
                END,
                updated_at = excluded.updated_at
            """,
            (user_id, display_name, now, now),
        )
        self._conn.commit()

    def create_project(
        self,
        user_id: str,
        name: str,
        description: str = "",
        *,
        project_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Project:
        """Create a persistent project for a user."""
        self.ensure_user(user_id)

        project_id = project_id or uuid.uuid4().hex[:16]
        now = time.time()

        self._conn.execute(
            """
            INSERT INTO projects
                (project_id, user_id, name, description,
                 created_at, updated_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                user_id,
                name,
                description,
                now,
                now,
                json.dumps(metadata or {}),
            ),
        )
        self._conn.commit()

        return Project(
            project_id=project_id,
            user_id=user_id,
            name=name,
            description=description,
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )

    def get_project(self, project_id: str) -> Optional[Project]:
        """Return a project by ID."""
        row = self._conn.execute(
            """
            SELECT project_id, user_id, name, description,
                   created_at, updated_at, metadata
            FROM projects
            WHERE project_id = ?
            """,
            (project_id,),
        ).fetchone()

        if not row:
            return None

        return Project(
            project_id=row[0],
            user_id=row[1],
            name=row[2],
            description=row[3] or "",
            created_at=row[4] or 0.0,
            updated_at=row[5] or 0.0,
            metadata=json.loads(row[6]) if row[6] else {},
        )

    def list_projects(
        self,
        user_id: str,
        *,
        limit: int = 50,
    ) -> List[Project]:
        """List projects belonging to a user."""
        rows = self._conn.execute(
            """
            SELECT project_id, user_id, name, description,
                   created_at, updated_at, metadata
            FROM projects
            WHERE user_id = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()

        return [
            Project(
                project_id=row[0],
                user_id=row[1],
                name=row[2],
                description=row[3] or "",
                created_at=row[4] or 0.0,
                updated_at=row[5] or 0.0,
                metadata=json.loads(row[6]) if row[6] else {},
            )
            for row in rows
        ]

    def create_session(
        self,
        user_id: str,
        project_id: str,
        *,
        title: str = "New chat",
        channel: str = "",
        channel_user_id: str = "",
        display_name: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Session:
        """Explicitly create a new persistent session."""
        project = self.get_project(project_id)

        if not project:
            raise ValueError(f"Unknown project: {project_id}")

        if project.user_id != user_id:
            raise ValueError("Project does not belong to user")

        return self._create_session(
            user_id,
            channel,
            channel_user_id,
            display_name,
            project_id=project_id,
            title=title,
            metadata=metadata,
        )

    def get_session(self, session_id: str) -> Optional[Session]:
        """Load a session directly by ID."""
        row = self._conn.execute(
            """
            SELECT session_id, user_id, project_id, display_name,
                   title, channel_ids, created_at, last_activity, metadata
            FROM sessions
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()

        if not row:
            return None

        project_name = ""
        if row[2]:
            project_row = self._conn.execute(
                "SELECT name FROM projects WHERE project_id = ?",
                (row[2],),
            ).fetchone()
            if project_row:
                project_name = project_row[0]

        return Session(
            session_id=row[0],
            identity=SessionIdentity(
                user_id=row[1],
                display_name=row[3] or "",
                channel_ids=json.loads(row[5]) if row[5] else {},
            ),
            project_id=row[2] or "",
            project_name=project_name,
            title=row[4] or "",
            messages=self._load_messages(row[0]),
            created_at=row[6] or 0.0,
            last_activity=row[7] or 0.0,
            metadata=json.loads(row[8]) if row[8] else {},
        )

    def delete_session(self, session_id: str) -> bool:
        """Delete one session and its messages. Returns whether it existed."""
        self._conn.execute(
            "DELETE FROM session_messages WHERE session_id = ?",
            (session_id,),
        )
        cursor = self._conn.execute(
            "DELETE FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def list_project_sessions(
        self,
        project_id: str,
        *,
        limit: int = 50,
    ) -> List[Session]:
        """List persistent conversation sessions in a project."""
        rows = self._conn.execute(
            """
            SELECT session_id
            FROM sessions
            WHERE project_id = ?
            ORDER BY last_activity DESC
            LIMIT ?
            """,
            (project_id, limit),
        ).fetchall()

        return [
            session for row in rows if (session := self.get_session(row[0])) is not None
        ]

    def get_or_create(
        self,
        user_id: str,
        *,
        channel: str = "",
        channel_user_id: str = "",
        display_name: str = "",
    ) -> Session:
        """Get the user's existing legacy/default session or create one.

        New code should prefer explicit project/session APIs.
        """
        self.ensure_user(user_id, display_name)

        # Preserve compatibility for existing callers by assigning them
        # to a private default project.
        project_row = self._conn.execute(
            "SELECT project_id FROM projects WHERE user_id = ? "
            "ORDER BY created_at LIMIT 1",
            (user_id,),
        ).fetchone()

        if project_row:
            default_project_id = project_row[0]
        else:
            default_project_id = self.create_project(
                user_id,
                "Default",
                "Default OpenJarvis project",
            ).project_id

        row = self._conn.execute(
            "SELECT session_id, user_id, project_id, display_name,"
            " title, channel_ids, created_at, last_activity,"
            " metadata "
            "FROM sessions WHERE user_id = ?"
            " ORDER BY last_activity DESC LIMIT 1",
            (user_id,),
        ).fetchone()

        if row:
            session_id = row[0]
            project_id = row[2] or default_project_id
            title = row[4] or "New chat"

            # Ensure legacy sessions are attached to the user's
            # compatibility/default project.
            if not row[2]:
                self._conn.execute(
                    "UPDATE sessions SET project_id = ? WHERE session_id = ?",
                    (default_project_id, session_id),
                )

            channel_ids = json.loads(row[5]) if row[5] else {}
            now = time.time()

            if channel and channel_user_id:
                channel_ids[channel] = channel_user_id

            self._conn.execute(
                "UPDATE sessions SET project_id = ?, channel_ids = ?,"
                " last_activity = ? WHERE session_id = ?",
                (
                    project_id,
                    json.dumps(channel_ids),
                    now,
                    session_id,
                ),
            )
            self._conn.commit()

            messages = self._load_messages(session_id)

            project = self.get_project(project_id)

            return Session(
                session_id=session_id,
                identity=SessionIdentity(
                    user_id=row[1],
                    display_name=row[3] or display_name,
                    channel_ids=channel_ids,
                ),
                project_id=project_id,
                project_name=project.name if project else "",
                title=title,
                messages=messages,
                created_at=row[6] or 0.0,
                last_activity=now,
                metadata=json.loads(row[8]) if row[8] else {},
            )

        return self._create_session(
            user_id,
            channel,
            channel_user_id,
            display_name,
            project_id=default_project_id,
            title="New chat",
        )

    def _create_session(
        self,
        user_id: str,
        channel: str,
        channel_user_id: str,
        display_name: str,
        *,
        project_id: str = "",
        title: str = "New chat",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Session:
        session_id = uuid.uuid4().hex[:16]
        now = time.time()
        channel_ids = {channel: channel_user_id} if channel and channel_user_id else {}
        self._conn.execute(
            "INSERT INTO sessions (session_id, user_id,"
            " project_id, display_name, title, channel_ids,"
            " created_at, last_activity, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                user_id,
                project_id,
                display_name,
                title,
                json.dumps(channel_ids),
                now,
                now,
                json.dumps(metadata or {}),
            ),
        )
        self._conn.commit()
        project = self.get_project(project_id) if project_id else None

        return Session(
            session_id=session_id,
            identity=SessionIdentity(
                user_id=user_id,
                display_name=display_name,
                channel_ids=channel_ids,
            ),
            project_id=project_id,
            project_name=project.name if project else "",
            title=title,
            created_at=now,
            last_activity=now,
            metadata=metadata or {},
        )

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        channel: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist a message to a session."""
        self._conn.execute(
            "INSERT INTO session_messages"
            " (session_id, role, content,"
            " channel, timestamp, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                session_id,
                role,
                content,
                channel,
                time.time(),
                json.dumps(metadata or {}),
            ),
        )
        title = content[:50] + ("..." if len(content) > 50 else "")
        self._conn.execute(
            "UPDATE sessions SET last_activity = ?, title = CASE "
            "WHEN ? = 'user' AND (title = '' OR title = 'New chat') "
            "THEN ? ELSE title END WHERE session_id = ?",
            (time.time(), role, title, session_id),
        )
        self._conn.commit()

        # Check if consolidation is needed
        count = self._conn.execute(
            "SELECT COUNT(*) FROM session_messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        if count > self._consolidation_threshold:
            self.consolidate(session_id)

    def consolidate(self, session_id: str) -> None:
        """Consolidate old messages: summarize oldest half, keep recent half."""
        messages = self._load_messages(session_id)
        if len(messages) <= self._consolidation_threshold // 2:
            return

        split = len(messages) // 2
        old_messages = messages[:split]

        # Create summary of old messages
        summary_parts = []
        for msg in old_messages[:10]:  # summarize first 10 of old batch
            summary_parts.append(f"[{msg.role}] {msg.content[:100]}")
        summary = "Session history summary:\n" + "\n".join(summary_parts)

        # Delete old messages
        oldest_ts = old_messages[-1].timestamp if old_messages else 0
        self._conn.execute(
            "DELETE FROM session_messages WHERE session_id = ? AND timestamp <= ?",
            (session_id, oldest_ts),
        )
        # Insert summary as system message
        self._conn.execute(
            "INSERT INTO session_messages"
            " (session_id, role, content,"
            " channel, timestamp) "
            "VALUES (?, 'system', ?, '', ?)",
            (session_id, summary, time.time()),
        )
        self._conn.commit()

    def decay(self, max_age_hours: Optional[float] = None) -> int:
        """Remove sessions older than max_age_hours. Returns count removed."""
        age = max_age_hours or self._max_age_hours
        cutoff = time.time() - (age * 3600)
        cur = self._conn.execute(
            "SELECT session_id FROM sessions WHERE last_activity < ?",
            (cutoff,),
        )
        session_ids = [row[0] for row in cur.fetchall()]
        for sid in session_ids:
            self._conn.execute(
                "DELETE FROM session_messages WHERE session_id = ?",
                (sid,),
            )
            self._conn.execute(
                "DELETE FROM sessions WHERE session_id = ?",
                (sid,),
            )
        self._conn.commit()
        return len(session_ids)

    def link_channel(self, session_id: str, channel: str, channel_user_id: str) -> None:
        """Link a channel identity to an existing session."""
        row = self._conn.execute(
            "SELECT channel_ids FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row:
            channel_ids = json.loads(row[0]) if row[0] else {}
            channel_ids[channel] = channel_user_id
            self._conn.execute(
                "UPDATE sessions SET channel_ids = ? WHERE session_id = ?",
                (json.dumps(channel_ids), session_id),
            )
            self._conn.commit()

    def list_sessions(
        self,
        *,
        user_id: Optional[str] = None,
        project_id: Optional[str] = None,
        active_only: bool = True,
        limit: int = 50,
    ) -> List[Session]:
        """List persistent sessions, optionally filtered by user/project."""
        sql = (
            "SELECT s.session_id, s.user_id, s.project_id, "
            "s.display_name, s.title, s.channel_ids, "
            "s.created_at, s.last_activity, s.metadata, "
            "p.name "
            "FROM sessions s "
            "LEFT JOIN projects p ON s.project_id = p.project_id"
        )

        conditions = []
        params: list = []

        if user_id:
            conditions.append("s.user_id = ?")
            params.append(user_id)

        if project_id:
            conditions.append("s.project_id = ?")
            params.append(project_id)

        if active_only:
            cutoff = time.time() - (self._max_age_hours * 3600)
            conditions.append("s.last_activity >= ?")
            params.append(cutoff)

        if conditions:
            sql += " WHERE " + " AND ".join(conditions)

        sql += " ORDER BY s.last_activity DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()

        sessions = []
        for row in rows:
            sessions.append(
                Session(
                    session_id=row[0],
                    identity=SessionIdentity(
                        user_id=row[1],
                        display_name=row[3] or "",
                        channel_ids=json.loads(row[5]) if row[5] else {},
                    ),
                    project_id=row[2],
                    project_name=row[9] or "",
                    title=row[4] or "",
                    created_at=row[6] or 0.0,
                    last_activity=row[7] or 0.0,
                    metadata=json.loads(row[8]) if row[8] else {},
                    messages=self._load_messages(row[0]),
                )
            )

        return sessions

    def _load_messages(self, session_id: str) -> List[SessionMessage]:
        rows = self._conn.execute(
            "SELECT role, content, channel, timestamp, metadata "
            "FROM session_messages WHERE session_id = ? ORDER BY timestamp",
            (session_id,),
        ).fetchall()
        return [
            SessionMessage(
                role=row[0],
                content=row[1],
                channel=row[2] or "",
                timestamp=row[3] or 0.0,
                metadata=json.loads(row[4]) if row[4] else {},
            )
            for row in rows
        ]

    def close(self) -> None:
        self._conn.close()


__all__ = ["Project", "Session", "SessionIdentity", "SessionMessage", "SessionStore"]
