"""SQLite persistence for the formal conversation archive.

The archive is the product's only official memory mechanism in this release:
sessions, full message history, citations and prompt versions live locally.
All operations run off the event loop in a worker thread; SQLite WAL mode allows
the web UI and the API to read while a turn is being written.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from omninexus.core.models import (
    Citation,
    SessionRecord,
    StoredMessage,
)
from omninexus.core.tokenizer import estimate_message_tokens
from omninexus.logging import get_logger

log = get_logger("storage")
T = TypeVar("T")

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    model TEXT,
    prompt_profile TEXT,
    web_search INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    parts TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    metadata TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session_created
    ON messages(session_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_source_event
    ON messages(source_event_id) WHERE source_event_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS citations (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    snippet TEXT NOT NULL DEFAULT '',
    rank INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'web',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_citations_event ON citations(event_id);

CREATE TABLE IF NOT EXISTS prompt_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    builtin INTEGER NOT NULL DEFAULT 0,
    active_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prompt_versions (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES prompt_profiles(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    system_template TEXT NOT NULL,
    change_note TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(profile_id, version)
);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._write_lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def _init() -> None:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(_SCHEMA)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            finally:
                conn.close()

        await asyncio.to_thread(_init)

    async def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        def _run() -> list[sqlite3.Row]:
            conn = self._connect()
            try:
                return list(conn.execute(sql, params).fetchall())
            finally:
                conn.close()

        return await asyncio.to_thread(_run)

    async def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        def _run() -> sqlite3.Row | None:
            conn = self._connect()
            try:
                return conn.execute(sql, params).fetchone()
            finally:
                conn.close()

        return await asyncio.to_thread(_run)

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        async with self._write_lock:

            def _run() -> int:
                conn = self._connect()
                try:
                    cur = conn.execute(sql, params)
                    return cur.rowcount
                finally:
                    conn.close()

            return await asyncio.to_thread(_run)

    async def transaction(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        async with self._write_lock:

            def _run() -> T:
                conn = self._connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    result = fn(conn)
                    conn.execute("COMMIT")
                    return result
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                finally:
                    conn.close()

            return await asyncio.to_thread(_run)

    # ----- sessions -----------------------------------------------------
    async def create_session(self, session: SessionRecord) -> None:
        await self.execute(
            """INSERT INTO sessions(id, title, model, prompt_profile, web_search,
                                   created_at, updated_at, metadata)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session.id,
                session.title,
                session.model,
                session.prompt_profile,
                1 if session.web_search else 0,
                session.created_at,
                session.updated_at,
                json.dumps(session.metadata, ensure_ascii=False),
            ),
        )

    async def get_session(self, session_id: str) -> SessionRecord | None:
        row = await self.fetchone("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return _row_to_session(row) if row else None

    async def list_sessions(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        rows = await self.fetchall(
            """SELECT s.*,
                      (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count,
                      (SELECT m.content FROM messages m WHERE m.session_id = s.id
                       ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1) AS last_message
               FROM sessions s
               ORDER BY s.updated_at DESC
               LIMIT ? OFFSET ?""",
            (max(1, min(limit, 200)), max(0, offset)),
        )
        result = []
        for row in rows:
            session = _row_to_session(row).model_dump()
            session["message_count"] = row["message_count"]
            session["last_message"] = (row["last_message"] or "")[:160]
            result.append(session)
        return result

    async def update_session(self, session_id: str, fields: dict[str, Any]) -> bool:
        allowed = {"title", "model", "prompt_profile", "web_search", "updated_at", "metadata"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return False
        if "web_search" in updates:
            updates["web_search"] = 1 if updates["web_search"] else 0
        if "metadata" in updates:
            updates["metadata"] = json.dumps(updates["metadata"], ensure_ascii=False)
        columns = ", ".join(f"{key} = ?" for key in updates)
        params = (*updates.values(), session_id)
        count = await self.execute(f"UPDATE sessions SET {columns} WHERE id = ?", params)
        return count > 0

    async def delete_session(self, session_id: str) -> bool:
        count = await self.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return count > 0

    # ----- messages -----------------------------------------------------
    async def add_message(self, message: StoredMessage, source_event_id: str | None = None) -> None:
        await self.execute(
            """INSERT INTO messages(id, session_id, role, content, parts, created_at,
                                    token_estimate, metadata, source_event_id)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message.id,
                message.session_id,
                message.role,
                message.content,
                json.dumps([p.model_dump() for p in message.parts], ensure_ascii=False),
                message.created_at,
                message.token_estimate
                or estimate_message_tokens(
                    message.content,
                    sum(1 for part in message.parts if part.modality == "image"),
                ),
                json.dumps(message.metadata, ensure_ascii=False),
                source_event_id,
            ),
        )

    async def list_messages(
        self, session_id: str, limit: int = 500, before: str | None = None
    ) -> list[StoredMessage]:
        if before:
            rows = await self.fetchall(
                """SELECT * FROM messages WHERE session_id = ? AND created_at < ?
                   ORDER BY created_at ASC, rowid ASC LIMIT ?""",
                (session_id, before, limit),
            )
        else:
            rows = await self.fetchall(
                """SELECT * FROM (
                       SELECT m.*, m.rowid AS _row_order FROM messages m
                       WHERE m.session_id = ?
                       ORDER BY m.created_at DESC, m.rowid DESC LIMIT ?
                   ) ORDER BY created_at ASC, _row_order ASC""",
                (session_id, limit),
            )
        return [_row_to_message(row) for row in rows]

    async def get_message_by_source_event(self, event_id: str) -> StoredMessage | None:
        row = await self.fetchone("SELECT * FROM messages WHERE source_event_id = ?", (event_id,))
        return _row_to_message(row) if row else None

    async def count_messages(self, session_id: str) -> int:
        row = await self.fetchone(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?", (session_id,)
        )
        return int(row["n"]) if row else 0

    # ----- citations ----------------------------------------------------
    async def add_citations(
        self, session_id: str, event_id: str, citations: list[Citation]
    ) -> None:
        if not citations:
            return
        from omninexus.core.models import iso_now

        created = iso_now()
        params = [
            (
                str(uuid.uuid4()),
                event_id,
                session_id,
                c.url[:2048],
                c.title[:500],
                c.snippet[:2000],
                c.rank,
                c.source[:64],
                created,
            )
            for c in citations
        ]
        async with self._write_lock:

            def _run() -> None:
                conn = self._connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.executemany(
                        """INSERT INTO citations(id, event_id, session_id, url, title, snippet,
                                                 rank, source, created_at)
                           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        params,
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                finally:
                    conn.close()

            await asyncio.to_thread(_run)

    async def get_citations(self, event_id: str) -> list[dict[str, Any]]:
        rows = await self.fetchall(
            "SELECT * FROM citations WHERE event_id = ? ORDER BY rank ASC", (event_id,)
        )
        return [dict(row) for row in rows]

    async def get_session_citations(self, session_id: str) -> list[dict[str, Any]]:
        rows = await self.fetchall(
            "SELECT * FROM citations WHERE session_id = ? ORDER BY created_at ASC, rank ASC",
            (session_id,),
        )
        return [dict(row) for row in rows]


def _row_to_session(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        id=row["id"],
        title=row["title"],
        model=row["model"],
        prompt_profile=row["prompt_profile"],
        web_search=bool(row["web_search"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        metadata=json.loads(row["metadata"] or "{}"),
    )


def _row_to_message(row: sqlite3.Row) -> StoredMessage:
    from omninexus.core.models import ContentPart

    parts_raw = json.loads(row["parts"] or "[]")
    parts = [ContentPart.model_validate(part) for part in parts_raw]
    return StoredMessage(
        id=row["id"],
        session_id=row["session_id"],
        role=row["role"],
        content=row["content"],
        parts=parts,
        created_at=row["created_at"],
        token_estimate=row["token_estimate"],
        metadata=json.loads(row["metadata"] or "{}"),
    )
