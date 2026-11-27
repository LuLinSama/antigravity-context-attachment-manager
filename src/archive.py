import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager


PAGE_SIZE = 20


def json_bytes(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")


class Archive:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "attachments.sqlite3"
        self.path.touch(mode=0o600, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS attachments (
                    scope TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
                    mime TEXT NOT NULL, payload BLOB NOT NULL, PRIMARY KEY (scope, id)
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
                UPDATE sessions SET mode='auto' WHERE mode='manual';
            """)
            columns = {column[1] for column in db.execute("PRAGMA table_info(sessions)")}
            if "active_turn_key" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN active_turn_key TEXT")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def save(db, scope, name, mime, part):
        payload = json.dumps(part, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
        identity = hashlib.sha256(payload).hexdigest()[:32]
        attachment_id = "CAM-" + identity
        row = db.execute("SELECT name FROM attachments WHERE scope = ? AND id = ?", (scope, attachment_id)).fetchone()
        if row is not None:
            return attachment_id, row[0]
        db.execute("INSERT OR IGNORE INTO attachments VALUES (?, ?, ?, ?, ?)", (scope, attachment_id, name, mime, payload))
        row = db.execute("SELECT name FROM attachments WHERE scope = ? AND id = ?", (scope, attachment_id)).fetchone()
        return attachment_id, row[0]

    @staticmethod
    def load(db, scope, attachment_id):
        row = db.execute("SELECT name, mime, payload FROM attachments WHERE scope = ? AND id = ?", (scope, attachment_id)).fetchone()
        if row is None:
            return None
        name, mime, payload = row
        return name, mime, json.loads(payload)

    @staticmethod
    def list(db, scope, query, offset):
        page_size = 25
        rows = db.execute("""
            SELECT id, name, mime FROM attachments
            WHERE scope = ? AND instr(lower(name || ' ' || id || ' ' || mime), lower(?)) > 0
            ORDER BY rowid DESC LIMIT ? OFFSET ?
        """, (scope, query, page_size + 1, offset)).fetchall()
        return {
            "attachments": [{"id": row[0], "name": row[1], "mimeType": row[2]} for row in rows[:page_size]],
            "nextOffset": offset + page_size if len(rows) > page_size else None,
        }

    @staticmethod
    def observe(db, scope, turn, prompt_fingerprint=None, *, active_turn_key=None):
        db.execute("""
            INSERT INTO sessions(scope, turn, updated, active_turn_key) VALUES (?, ?, ?, ?)
            ON CONFLICT(scope) DO UPDATE SET turn=excluded.turn, updated=excluded.updated,
                active_turn_key=COALESCE(excluded.active_turn_key, sessions.active_turn_key)
        """, (scope, turn, int(time.time()), active_turn_key))
        if prompt_fingerprint is not None:
            db.execute("""
                INSERT INTO session_names(scope, prompt_fingerprint) VALUES (?, ?)
                ON CONFLICT(scope) DO UPDATE SET prompt_fingerprint=excluded.prompt_fingerprint
                WHERE conversation_id IS NULL AND prompt_fingerprint IS NOT excluded.prompt_fingerprint
            """, (scope, prompt_fingerprint))
        return db.execute("SELECT mode FROM sessions WHERE scope = ?", (scope,)).fetchone()[0]

    def record_forward(self, scope, received, forwarded, report):
        with self.connect() as db:
            db.execute("""
                UPDATE sessions SET received=?, forwarded=?, omitted=?, restored=?, requests=requests+1,
                total_received=total_received+?, total_forwarded=total_forwarded+? WHERE scope=?
            """, (received, forwarded, report.omitted, report.restored, received, forwarded, scope))
            if report.turn_key is not None:
                db.execute("""
                    INSERT INTO turn_traffic(scope, turn_key, received, forwarded) VALUES (?, ?, ?, ?)
                    ON CONFLICT(scope, turn_key) DO UPDATE SET
                        received=turn_traffic.received+excluded.received,
                        forwarded=turn_traffic.forwarded+excluded.forwarded
                """, (scope, report.turn_key, received, forwarded))

    @staticmethod
    def observe_turns(db, scope, turn_keys):
        known = dict(db.execute("SELECT turn_key, number FROM turns WHERE scope=?", (scope,)))
        number = max(known.values(), default=0)
        for turn_key in turn_keys:
            if turn_key not in known:
                number += 1
                db.execute("INSERT INTO turns VALUES (?, ?, ?)", (scope, turn_key, number))
                known[turn_key] = number

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
                SELECT s.scope, label, mode, updated, received, forwarded, omitted, restored, requests, s.turn,
                       n.prompt_fingerprint AS promptFingerprint, n.conversation_id AS conversationId,
                       total_received AS totalReceived, total_forwarded AS totalForwarded,
                       COALESCE(active.number, (SELECT MAX(number) FROM turns WHERE scope=s.scope)) AS currentTurn,
                       (SELECT COUNT(*) FROM attachments a WHERE a.scope=s.scope) AS count,
                       (SELECT name FROM attachments a WHERE a.scope=s.scope AND name<>'' ORDER BY a.rowid DESC LIMIT 1) AS sample
                FROM sessions s LEFT JOIN session_names n USING (scope)
                LEFT JOIN turns active ON active.scope=s.scope AND active.turn_key=s.active_turn_key
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
            SELECT a.id, a.name, a.mime, t.number, COALESCE(u.sources,0), traffic.received, traffic.forwarded
            FROM attachments a LEFT JOIN attachment_options o USING (scope, id)
            LEFT JOIN turn_attachments u USING (scope, id) LEFT JOIN turns t USING (scope, turn_key)
            LEFT JOIN turn_traffic traffic ON traffic.scope=t.scope AND traffic.turn_key=t.turn_key
            WHERE a.scope=? AND a.mime LIKE ? AND (?<>'document' OR a.mime LIKE 'application/%' OR a.mime LIKE 'text/%')
                  AND instr(lower(a.name || ' ' || a.id || ' ' || a.mime || ' ' ||
                  COALESCE(o.note,'') || ' ' || COALESCE(o.collection,'')), lower(?))>0
            ORDER BY t.number DESC, a.rowid DESC LIMIT ? OFFSET ?
        """, (scope, any_kind.get(kind, '%'), kind, query, PAGE_SIZE + 1, offset)).fetchall()
        fields = ("id", "name", "mimeType", "turn", "sources", "turnReceived", "turnForwarded")
        return {"attachments": [dict(zip(fields, row)) for row in rows[:PAGE_SIZE]],
                "nextOffset": offset + PAGE_SIZE if len(rows) > PAGE_SIZE else None}

    @staticmethod
    def set_session(db, scope, mode, label):
        db.execute("UPDATE sessions SET mode=?, label=? WHERE scope=?", (mode, label, scope))

