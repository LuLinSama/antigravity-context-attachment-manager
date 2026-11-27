import base64
import binascii
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from conversation_names import fingerprint
from media import EXTENSIONS, INLINE, READ, media_part


PAGE_SIZE = 20


def json_bytes(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")


class Archive:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory.resolve() / "attachments.sqlite3"
        self.path.touch(mode=0o600, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS attachments (
                    scope TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
                    mime TEXT NOT NULL, payload BLOB NOT NULL,
                    file_digest TEXT, file_size INTEGER, PRIMARY KEY (scope, id)
                )
            """)
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS sessions (
                    scope TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL DEFAULT 'auto', turn TEXT,
                    updated INTEGER NOT NULL DEFAULT 0,
                    received INTEGER NOT NULL DEFAULT 0, forwarded INTEGER NOT NULL DEFAULT 0,
                    omitted INTEGER NOT NULL DEFAULT 0, restored INTEGER NOT NULL DEFAULT 0,
                    requests INTEGER NOT NULL DEFAULT 0,
                    total_received INTEGER NOT NULL DEFAULT 0, total_forwarded INTEGER NOT NULL DEFAULT 0,
                    active_turn_key TEXT
                );
                CREATE TABLE IF NOT EXISTS attachment_options (
                    scope TEXT NOT NULL, id TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
                    collection TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0,
                    queued TEXT, queued_after TEXT, PRIMARY KEY (scope, id)
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
                CREATE TABLE IF NOT EXISTS turn_traffic (
                    scope TEXT NOT NULL, turn_key TEXT NOT NULL,
                    received INTEGER NOT NULL, forwarded INTEGER NOT NULL,
                    PRIMARY KEY (scope, turn_key)
                );
                CREATE TABLE IF NOT EXISTS turn_attachments (
                    scope TEXT NOT NULL, turn_key TEXT NOT NULL, id TEXT NOT NULL,
                    sources INTEGER NOT NULL, PRIMARY KEY (scope, turn_key, id)
                );
                CREATE INDEX IF NOT EXISTS turn_attachments_by_file ON turn_attachments(scope, id);
                INSERT OR IGNORE INTO sessions(scope) SELECT DISTINCT scope FROM attachments;
            """)
            columns = {column[1] for column in db.execute("PRAGMA table_info(sessions)")}
            if "active_turn_key" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN active_turn_key TEXT")
            columns = {column[1] for column in db.execute("PRAGMA table_info(attachments)")}
            if "file_digest" not in columns:
                db.execute("ALTER TABLE attachments ADD COLUMN file_digest TEXT")
            if "file_size" not in columns:
                db.execute("ALTER TABLE attachments ADD COLUMN file_size INTEGER")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def original_path(self, mime, digest):
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid archived file digest")
        directory = self.path.parent / "media"
        path = directory / (digest + EXTENSIONS.get(mime.lower(), ".bin"))
        if path.resolve() != path:
            raise OSError("Archived media path was redirected")
        return path

    def store_inline(self, mime, part):
        media = media_part(part)
        if media is None or media[0] not in INLINE:
            return None
        key, inline, _ = media
        try:
            original = base64.b64decode(inline["data"], validate=True)
        except (binascii.Error, ValueError):
            return None
        digest = hashlib.sha256(original).hexdigest()
        path = self.original_path(mime, digest)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        if not path.is_file():
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
                    stream.close()
                    # Publish the complete file before committing a database reference.
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
        metadata = dict(inline)
        del metadata["data"]
        return json_bytes({"inlineKey": key, "part": {**part, key: metadata}}), digest, len(original)

    def migrate_original(self, db, scope, attachment_id):
        # Recheck under the write lock in case another panel click migrated it.
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT mime, payload, file_digest FROM attachments WHERE scope=? AND id=?",
                         (scope, attachment_id)).fetchone()
        if row is None:
            return None
        mime, payload, digest = row
        if digest is not None:
            return digest
        part = json.loads(payload)
        stored = self.store_inline(mime, part)
        if stored is None:
            return None
        db.execute("""
            UPDATE attachments SET payload=?, file_digest=?, file_size=?
            WHERE scope=? AND id=? AND file_digest IS NULL
        """, (*stored, scope, attachment_id))
        return stored[1]

    def load(self, db, scope, attachment_id):
        row = db.execute("""
            SELECT name, mime, payload, file_digest, file_size FROM attachments WHERE scope=? AND id=?
        """, (scope, attachment_id)).fetchone()
        if row is None:
            return None
        name, mime, payload, digest, size = row
        part = json.loads(payload)
        if digest is not None:
            try:
                with self.original_path(mime, digest).open("rb") as stream:
                    if os.fstat(stream.fileno()).st_size != size or size < 1:
                        return None
                    original = stream.read(size + 1)
            except (OSError, ValueError):
                return None
            if hashlib.sha256(original).hexdigest() != digest:
                return None
            key, part = part["inlineKey"], part["part"]
            part[key]["data"] = base64.b64encode(original).decode("ascii")
        return name, mime, part

    def record_local_read(self, conversation_id, path, mime, turn, updated):
        scope = fingerprint(conversation_id)
        attachment_id = "CAM-" + fingerprint("local:" + os.path.normcase(str(path)))[:32]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM sessions WHERE scope=?", (scope,)).fetchone():
                linked = db.execute("SELECT scope FROM session_names WHERE lower(conversation_id)=? LIMIT 2",
                                    (conversation_id,)).fetchall()
                if len(linked) == 1:
                    scope = linked[0][0]
            db.execute("""
                INSERT INTO sessions(scope, updated) VALUES (?, ?)
                ON CONFLICT(scope) DO UPDATE SET updated=MAX(updated, excluded.updated)
            """, (scope, updated))
            self.link_conversations(db, {scope: conversation_id})
            db.execute("""
                INSERT OR IGNORE INTO attachments(scope, id, name, mime, payload) VALUES (?, ?, ?, ?, X'7B7D')
            """, (scope, attachment_id, path.name, mime))
            db.execute("INSERT OR IGNORE INTO local_files VALUES (?, ?, ?)", (scope, attachment_id, str(path)))
            if turn is not None:
                number, key = turn
                db.execute("INSERT OR IGNORE INTO turns VALUES (?, ?, ?)", (scope, key, number))
                if db.execute("SELECT number FROM turns WHERE scope=? AND turn_key=?", (scope, key)).fetchone() == (number,):
                    self.record_occurrence(db, scope, key, attachment_id, READ)

    @staticmethod
    def record_occurrence(db, scope, turn_key, attachment_id, source):
        if turn_key is not None:
            db.execute("""
                INSERT INTO turn_attachments(scope, turn_key, id, sources) VALUES (?, ?, ?, ?)
                ON CONFLICT(scope, turn_key, id) DO UPDATE SET sources=sources | excluded.sources
                WHERE (sources & excluded.sources)<>excluded.sources
            """, (scope, turn_key, attachment_id, source))

    @staticmethod
    def sessions(db):
        old_factory = db.row_factory
        db.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in db.execute("""
                SELECT s.scope, label, updated, s.turn,
                       n.prompt_fingerprint AS promptFingerprint, n.conversation_id AS conversationId,
                       (SELECT COUNT(*) FROM turn_attachments u WHERE u.scope=s.scope) AS occurrences,
                       (SELECT COUNT(*) FROM attachments a WHERE a.scope=s.scope) AS count,
                       (SELECT name FROM attachments a WHERE a.scope=s.scope AND name<>'' ORDER BY a.rowid DESC LIMIT 1) AS sample
                FROM sessions s LEFT JOIN session_names n USING (scope)
                ORDER BY updated DESC, s.scope
            """)]
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
        any_kind = {"image": "image/%", "video": "video/%", "audio": "audio/%"}
        rows = db.execute("""
            SELECT a.id, a.name, a.mime, t.number, COALESCE(u.sources,CASE WHEN l.id IS NULL THEN 0 ELSE 2 END)
            FROM attachments a LEFT JOIN attachment_options o USING (scope, id)
            LEFT JOIN local_files l USING (scope, id)
            LEFT JOIN turn_attachments u USING (scope, id) LEFT JOIN turns t USING (scope, turn_key)
            WHERE a.scope=? AND a.mime LIKE ? AND (?<>'document' OR a.mime LIKE 'application/%' OR a.mime LIKE 'text/%')
                  AND instr(lower(a.name || ' ' || a.id || ' ' || a.mime || ' ' ||
                  COALESCE(o.note,'') || ' ' || COALESCE(o.collection,'')), lower(?))>0
            ORDER BY t.number DESC, a.rowid DESC LIMIT ? OFFSET ?
        """, (scope, any_kind.get(kind, '%'), kind, query, PAGE_SIZE + 1, offset)).fetchall()
        fields = ("id", "name", "mimeType", "turn", "sources")
        return {"attachments": [dict(zip(fields, row)) for row in rows[:PAGE_SIZE]],
                "nextOffset": offset + PAGE_SIZE if len(rows) > PAGE_SIZE else None}
