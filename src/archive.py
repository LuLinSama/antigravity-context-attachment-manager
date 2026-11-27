"""Local path-only archive; old database tables are left intact."""

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from conversation_names import fingerprint
from media import DELEGATED_READ
from media_delegation import delegated_turn, parent_conversation


PAGE_SIZE = 20


class Archive:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory.resolve() / "attachments.sqlite3"
        self.path.touch(mode=0o600, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS attachments (
                    scope TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
                    mime TEXT NOT NULL, payload BLOB NOT NULL, PRIMARY KEY (scope, id)
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    scope TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '', updated INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS session_names (
                    scope TEXT PRIMARY KEY, prompt_fingerprint TEXT, conversation_id TEXT
                );
                CREATE TABLE IF NOT EXISTS turns (
                    scope TEXT NOT NULL, turn_key TEXT NOT NULL, number INTEGER NOT NULL,
                    PRIMARY KEY (scope, turn_key), UNIQUE (scope, number)
                );
                CREATE TABLE IF NOT EXISTS local_files (
                    scope TEXT NOT NULL, id TEXT NOT NULL, path TEXT NOT NULL,
                    PRIMARY KEY (scope, id)
                );
                CREATE TABLE IF NOT EXISTS turn_attachments (
                    scope TEXT NOT NULL, turn_key TEXT NOT NULL, id TEXT NOT NULL,
                    sources INTEGER NOT NULL, PRIMARY KEY (scope, turn_key, id)
                );
                CREATE TABLE IF NOT EXISTS pending_reads (
                    child TEXT PRIMARY KEY, parent TEXT NOT NULL, path TEXT NOT NULL,
                    mime TEXT NOT NULL, updated INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS pending_reads_parent ON pending_reads(parent, updated);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=1)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def touch(db, conversation_id, updated):
        scope = fingerprint(conversation_id)
        if not db.execute("SELECT 1 FROM sessions WHERE scope=?", (scope,)).fetchone():
            linked = db.execute("SELECT scope FROM session_names WHERE lower(conversation_id)=? LIMIT 2",
                                (conversation_id,)).fetchall()
            if len(linked) == 1:
                scope = linked[0][0]
        db.execute("""
            INSERT INTO sessions(scope, updated) VALUES (?, ?)
            ON CONFLICT(scope) DO UPDATE SET updated=excluded.updated WHERE updated<excluded.updated
        """, (scope, updated))
        Archive.link_conversations(db, {scope: conversation_id})
        return scope

    def queue_read(self, child, parent, path, mime, updated):
        with self.connect() as db:
            db.execute("DELETE FROM pending_reads WHERE updated<=?", (updated - 86400,))
            db.execute("INSERT OR IGNORE INTO pending_reads VALUES (?, ?, ?, ?, ?)", (child, parent, str(path), mime, updated))

    def reference(self, db, conversation_id, path, mime, turn, updated):
        number, key = turn
        scope = self.touch(db, conversation_id, updated)
        attachment_id = "CAM-" + fingerprint("local:" + os.path.normcase(str(path)))[:32]
        db.execute("INSERT OR IGNORE INTO turns VALUES (?, ?, ?)", (scope, key, number))
        if db.execute("SELECT number FROM turns WHERE scope=? AND turn_key=?", (scope, key)).fetchone() != (number,):
            return False
        db.execute("INSERT OR IGNORE INTO attachments(scope, id, name, mime, payload) VALUES (?, ?, ?, ?, X'7B7D')",
                   (scope, attachment_id, path.name, mime))
        db.execute("INSERT OR IGNORE INTO local_files VALUES (?, ?, ?)", (scope, attachment_id, str(path)))
        db.execute("""
            INSERT INTO turn_attachments(scope, turn_key, id, sources) VALUES (?, ?, ?, ?)
            ON CONFLICT(scope, turn_key, id) DO UPDATE SET sources=sources | excluded.sources
            WHERE (sources & excluded.sources)<>excluded.sources
        """, (scope, key, attachment_id, DELEGATED_READ))
        return True

    @staticmethod
    def sessions(db):
        old_factory = db.row_factory
        db.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in db.execute("""
                WITH library AS (
                    SELECT scope, COUNT(*) AS occurrences, COUNT(DISTINCT id) AS count, MIN(name) AS sample
                    FROM turn_attachments JOIN local_files USING (scope, id) JOIN attachments USING (scope, id)
                    WHERE (sources & ?) <> 0 GROUP BY scope
                )
                SELECT s.scope, label, updated, n.prompt_fingerprint AS promptFingerprint,
                       n.conversation_id AS conversationId, COALESCE(l.occurrences,0) AS occurrences,
                       COALESCE(l.count,0) AS count, l.sample
                FROM sessions s LEFT JOIN session_names n USING (scope) LEFT JOIN library l USING (scope)
                WHERE l.scope IS NOT NULL OR s.updated>0
                ORDER BY updated DESC, s.scope
            """, (DELEGATED_READ,))]
        finally:
            db.row_factory = old_factory

    @staticmethod
    def link_conversations(db, links):
        db.executemany("""
            INSERT INTO session_names(scope, conversation_id) VALUES (?, ?)
            ON CONFLICT(scope) DO UPDATE SET conversation_id=excluded.conversation_id, prompt_fingerprint=NULL
            WHERE conversation_id IS NULL
        """, links.items())

    @staticmethod
    def entries(db, scope, query, kind, offset):
        any_kind = {"image": "image/%", "video": "video/%", "audio": "audio/%", "document": "application/%"}
        rows = db.execute("""
            SELECT a.id, a.name, a.mime, t.number, ?
            FROM turn_attachments u JOIN local_files l USING (scope, id) JOIN attachments a USING (scope, id)
            JOIN turns t USING (scope, turn_key)
            WHERE a.scope=? AND (u.sources & ?) <> 0 AND a.mime LIKE ?
                  AND instr(lower(a.name || ' ' || a.id || ' ' || a.mime), lower(?))>0
            ORDER BY t.number DESC, a.rowid DESC LIMIT ? OFFSET ?
        """, (DELEGATED_READ, scope, DELEGATED_READ, any_kind.get(kind, '%'), query, PAGE_SIZE + 1, offset)).fetchall()
        fields = ("id", "name", "mimeType", "turn", "sources")
        return {"attachments": [dict(zip(fields, row)) for row in rows[:PAGE_SIZE]],
                "nextOffset": offset + PAGE_SIZE if len(rows) > PAGE_SIZE else None}


def reconcile_reads(archive, app_data, parent):
    with archive.connect() as db:
        pending = db.execute("""SELECT child, path, mime, updated FROM pending_reads
                                WHERE parent=? AND updated>? ORDER BY updated DESC LIMIT 32""",
                             (parent, int(time.time()) - 86400)).fetchall()
    for child, value, mime, updated in pending:
        if parent_conversation(app_data, child) != parent:
            continue
        turn = delegated_turn(app_data, parent, child)
        if turn is not None:
            with archive.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if archive.reference(db, parent, Path(value), mime, turn, updated):
                    db.execute("DELETE FROM pending_reads WHERE child=?", (child,))
