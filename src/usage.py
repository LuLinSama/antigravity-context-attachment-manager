"""Read-only uplink sampling and a local, cross-process invocation ledger."""

import json
import os
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass

from conversation_names import fingerprint
from media import source_file
from media_delegation import CAM_MEDIA, conversation_identity, parent_conversation, transcript_paths
from transcript_index import INDEX_PENDING, TranscriptIndex, delegated_turn, user_turn


GRANT_SECONDS = 300
MODES = ("save", "native")
UNKNOWN = object()
MAX_WINDOW = 30 * 60
MAX_RESPONSE = 128 * 1024
TAG = re.compile(r"[a-zA-Z0-9_.-]{1,80}\Z")


@dataclass(frozen=True)
class Metrics:
    enabled: bool = True
    port: int = 10812
    tag: str = "proxy"


def settings(directory):
    try:
        with (directory / "traffic.json").open("rb") as stream:
            raw = stream.read(4097)
        data = json.loads(raw) if len(raw) <= 4096 else None
        if not isinstance(data, dict):
            return None
        enabled, port, tag = data.get("enabled", True), data.get("port", 10812), data.get("outboundTag", "proxy")
        if (type(enabled) is bool and type(port) is int and 1 <= port <= 65535
                and isinstance(tag, str) and TAG.fullmatch(tag)):
            return Metrics(enabled, port, tag)
    except FileNotFoundError:
        return Metrics()
    except (OSError, ValueError, RecursionError):
        pass
    return None


def natural(value):
    return type(value) is int and 0 <= value < 2**63


def sample(config):
    from http import client

    # Fixed IPv4 loopback and path. No proxy, redirects, credentials, resets or request payloads.
    connection = client.HTTPConnection("127.0.0.1", config.port, timeout=.35)
    deadline = time.monotonic() + .7
    try:
        connection.request("GET", "/debug/vars", headers={"Connection": "close"})
        with connection.getresponse() as reply:
            if reply.status != 200:
                return None
            raw = bytearray()
            while len(raw) <= MAX_RESPONSE and time.monotonic() < deadline:
                piece = reply.read1(min(8192, MAX_RESPONSE + 1 - len(raw)))
                if not piece:
                    break
                raw.extend(piece)
            else:
                return None
        data = json.loads(raw)
        count = data["stats"]["outbound"][config.tag]["uplink"]
        if natural(count):
            mem = data.get("memstats")
            allocated = mem.get("TotalAlloc") if isinstance(mem, dict) else None
            return count, allocated if natural(allocated) else None
    except (OSError, ValueError, KeyError, TypeError, RecursionError, client.HTTPException):
        pass
    finally:
        connection.close()
    return None


def owner_for(app_data, cid, before, directory, identity=UNKNOWN):
    if identity is UNKNOWN:
        identity = conversation_identity(app_data, cid)
    if identity is not None and identity[1]:
        agent, parent = identity
        turn = delegated_turn(app_data, parent, cid, directory=directory) if agent == CAM_MEDIA else None
        if turn and not user_turn(app_data, cid, before, directory=directory):
            return parent, *turn, "child"
        return None
    if identity and identity[0] == CAM_MEDIA:
        return None
    turn = user_turn(app_data, cid, before, directory=directory)
    return (cid, *turn, "main") if turn else None


class Usage:
    def __init__(self, directory):
        self.path = directory / "usage.sqlite3"

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=1)) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] < 1:
                db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS leases (
                    child TEXT PRIMARY KEY, step INTEGER NOT NULL, path_key TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS closed_children (child TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS usage_resets (owner TEXT PRIMARY KEY, deleted_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS windows (
                    cid TEXT NOT NULL, invocation INTEGER NOT NULL, anchor INTEGER NOT NULL,
                    started REAL NOT NULL, state TEXT NOT NULL, overlap INTEGER NOT NULL DEFAULT 0,
                    port INTEGER NOT NULL, tag TEXT NOT NULL, baseline INTEGER, allocated INTEGER,
                    bytes INTEGER, owner TEXT, number INTEGER, turn_key TEXT, role TEXT,
                    PRIMARY KEY(cid, invocation, anchor)
                );
                CREATE INDEX IF NOT EXISTS windows_owner ON windows(owner, number);
                CREATE INDEX IF NOT EXISTS windows_state ON windows(state, started);
                CREATE TABLE IF NOT EXISTS media_mode (id INTEGER PRIMARY KEY CHECK(id=1), mode TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS media_grants (
                    owner TEXT PRIMARY KEY, path_key TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mode_notices (owner TEXT PRIMARY KEY, token TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS child_replies (
                    child TEXT PRIMARY KEY, invocation INTEGER NOT NULL, anchor INTEGER NOT NULL
                );
                PRAGMA user_version=1;
                COMMIT;
                """)
            with db:
                yield db

    @staticmethod
    def mode(db):
        row = db.execute("SELECT mode FROM media_mode WHERE id=1").fetchone()
        return row[0] if row and row[0] in MODES else "save"

    def policy(self, owner=None):
        if not self.path.is_file():
            return {"mode": "save", "grantUntil": None}
        with self.db() as db:
            mode = self.mode(db)
            row = db.execute("SELECT expires FROM media_grants WHERE owner=? AND expires>?",
                             (owner, time.time())).fetchone() if mode == "save" and owner else None
            return {"mode": mode, "grantUntil": row[0] if row else None}

    def set_mode(self, mode):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.mode(db) != mode:
                db.execute("INSERT OR REPLACE INTO media_mode VALUES (1, ?)", (mode,))
                db.execute("DELETE FROM media_grants")

    def grant(self, owner, path):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.mode(db) != "save":
                return None
            now = time.time()
            db.execute("DELETE FROM media_grants WHERE expires<=?", (now,))
            db.execute("INSERT OR REPLACE INTO media_grants VALUES (?, ?, ?)",
                       (owner, fingerprint(os.path.normcase(str(path))), now + GRANT_SECONDS))
            return now + GRANT_SECONDS

    def revoke(self, owner):
        if self.path.is_file():
            with self.db() as db:
                db.execute("DELETE FROM media_grants WHERE owner=?", (owner,))

    def allow_main_read(self, owner, value):
        if not self.path.is_file():
            return False
        with self.db() as db:
            if self.mode(db) == "native":
                return True
            grant = db.execute("SELECT path_key FROM media_grants WHERE owner=? AND expires>?",
                               (owner, time.time())).fetchone()
            if grant is None or (source := source_file(value)) is None:
                return False
            key = fingerprint(os.path.normcase(str(source[0])))
            if key != grant[0]:
                return False
            # Recheck expiry in the atomic consume statement; no retry exemption.
            return db.execute("DELETE FROM media_grants WHERE owner=? AND path_key=? AND expires>?",
                              (owner, key, time.time())).rowcount == 1

    def policy_notice(self, owner):
        if not self.path.is_file():
            return None
        with self.db() as db:
            mode = self.mode(db)
            grant = db.execute("SELECT expires FROM media_grants WHERE owner=? AND expires>?",
                               (owner, time.time())).fetchone() if mode == "save" else None
            token = f"grant:{grant[0]}" if grant else mode
            previous = db.execute("SELECT token FROM mode_notices WHERE owner=?", (owner,)).fetchone()
            if (not previous and token == "save") or previous == (token,):
                return None
            updated = db.execute("""INSERT INTO mode_notices VALUES (?, ?)
                ON CONFLICT(owner) DO UPDATE SET token=excluded.token WHERE token<>excluded.token""", (owner, token))
            return ("grant" if grant else mode) if updated.rowcount else None

    def allow_child_invocation(self, child, invocation, anchor):
        if not self.path.is_file():
            return True
        with self.db() as db:
            closed, read_step, prior_invocation, prior_anchor = db.execute("""
                SELECT EXISTS (SELECT 1 FROM closed_children WHERE child=?),
                       (SELECT step FROM leases WHERE child=?), r.invocation, r.anchor
                FROM (SELECT ? AS child) AS current LEFT JOIN child_replies r USING(child)
            """, (child, child, child)).fetchone()
            if closed:
                return False
            if prior_invocation is not None:
                return (invocation, anchor) == (prior_invocation, prior_anchor)
            if read_step is None or anchor <= read_step:
                return True  # initialNumSteps includes the completed zero-indexed read step.
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM closed_children WHERE child=?", (child,)).fetchone():
                return False
            db.execute("INSERT OR IGNORE INTO child_replies VALUES (?, ?, ?)", (child, invocation, anchor))
            return db.execute("SELECT invocation,anchor FROM child_replies WHERE child=?",
                              (child,)).fetchone() == (invocation, anchor)


    def clear(self, owner, deleted_at):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO usage_resets VALUES (?, ?)", (owner, deleted_at))
            db.execute("DELETE FROM windows WHERE (owner=? OR cid=?) AND started<=?", (owner, owner, deleted_at))
            db.execute("DELETE FROM media_grants WHERE owner=?", (owner,))

    @staticmethod
    def was_deleted(db, owner, started=None):
        return db.execute("SELECT 1 FROM usage_resets WHERE owner=? AND deleted_at>=COALESCE(?, deleted_at)",
                          (owner, started)).fetchone() is not None

    def close_child(self, child):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO closed_children VALUES (?)", (child,))
            db.execute("UPDATE windows SET state=CASE overlap WHEN 1 THEN 'overlap' ELSE 'incomplete' END WHERE cid=? AND state='active'", (child,))

    def closed(self, child):
        with self.db() as db:
            return db.execute("SELECT 1 FROM closed_children WHERE child=?", (child,)).fetchone() is not None

    def reserve(self, child, step, value):
        key = fingerprint(value)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM closed_children WHERE child=?", (child,)).fetchone():
                return False
            db.execute("INSERT OR IGNORE INTO leases VALUES (?, ?, ?)", (child, step, key))
            return db.execute("SELECT step, path_key FROM leases WHERE child=?", (child,)).fetchone() == (step, key)

    def reserved(self, child, step, value):
        with self.db() as db:
            return db.execute("SELECT step, path_key FROM leases WHERE child=?", (child,)).fetchone() == (step, fingerprint(value))

    def begin(self, key, owner, config):
        now = time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE windows SET state='incomplete' WHERE state='active' AND started<?", (now - MAX_WINDOW,))
            if db.execute("SELECT 1 FROM windows WHERE cid=? AND invocation=? AND anchor=?", key).fetchone():
                return
            overlap = db.execute("UPDATE windows SET overlap=1 WHERE state='active'").rowcount > 0
            if self.was_deleted(db, owner[0] if owner else key[0], now):
                return
            count, allocated = (None, None) if overlap else (sample(config) or (None, None))
            db.execute("""INSERT INTO windows(cid, invocation, anchor, started, state, overlap, port, tag,
                        baseline, allocated, owner, number, turn_key, role)
                        VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (*key, now, overlap, config.port, config.tag, count, allocated, *(owner or (None,) * 4)))

    def end(self, key, owner, config, resolve=None):
        with self.db() as db:
            if not config or not config.enabled:
                pending = db.execute("""
                    SELECT EXISTS(SELECT 1 FROM windows WHERE cid=? AND invocation=? AND anchor=? AND state='active')
                        OR EXISTS(SELECT 1 FROM child_replies r WHERE child=? AND invocation=? AND anchor=?
                                  AND NOT EXISTS(SELECT 1 FROM closed_children WHERE child=r.child))
                """, (*key, *key)).fetchone()[0]
                if not pending:
                    return  # A completed/disabled sample never needs a write lock.

            # Closing here shares the post-hook's DB connection, even without Metrics.
            def close_reply():
                db.execute("""INSERT OR IGNORE INTO closed_children SELECT child FROM child_replies
                              WHERE child=? AND invocation=? AND anchor=?""", key)

            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT state, overlap, port, tag, baseline, allocated, started, owner FROM windows
                                WHERE cid=? AND invocation=? AND anchor=?""", key).fetchone()
            if row is None:
                if config and config.enabled:
                    if owner is None and resolve is not None:
                        owner = resolve()
                    # Missing start: the unknown invocation could overlap any active window.
                    db.execute("UPDATE windows SET overlap=1 WHERE state='active'")
                    # A late post-hook without a start must not recreate deleted history.
                    if self.was_deleted(db, owner[0] if owner else key[0]):
                        close_reply()
                        return
                    db.execute("""INSERT INTO windows(cid, invocation, anchor, started, state, port, tag,
                                owner, number, turn_key, role) VALUES (?, ?, ?, ?, 'incomplete', ?, ?, ?, ?, ?, ?)""",
                               (*key, time.time(), config.port, config.tag, *(owner or (None,) * 4)))
                close_reply()
                return
            state, overlap, port, tag, count, allocated, started, existing_owner = row
            if state != "active":
                close_reply()
                return
            delta = None
            if overlap:
                state = "overlap"
            elif time.time() - started > MAX_WINDOW or not config or not config.enabled or (port, tag) != (config.port, config.tag):
                state = "incomplete"
            elif count is None or (current := sample(config)) is None:
                state = "unavailable"
            elif current[0] < count or (allocated is not None and current[1] is not None and current[1] < allocated):
                state = "reset"
            else:
                state, delta = "estimated", current[0] - count
            db.execute("UPDATE windows SET state=?, bytes=? WHERE cid=? AND invocation=? AND anchor=?", (state, delta, *key))
            if existing_owner is None and owner is None and resolve is not None:
                owner = resolve()
            if existing_owner is None and owner:
                self.assign(db, key, owner)
            close_reply()

    @staticmethod
    def assign(db, key, owner):
        # A child can be attributed only after its native launch has been confirmed.
        db.execute("""DELETE FROM windows WHERE cid=? AND invocation=? AND anchor=? AND owner IS NULL
                      AND started<=(SELECT deleted_at FROM usage_resets WHERE owner=?)""", (*key, owner[0]))
        db.execute("""UPDATE windows SET owner=?, number=?, turn_key=?, role=?
                      WHERE cid=? AND invocation=? AND anchor=? AND owner IS NULL""", (*owner, *key))

    def reap(self, app_data, parent):
        with self.db() as db:
            active = {child for child, in db.execute("""SELECT DISTINCT cid FROM windows
                      WHERE owner=? AND role='child' AND state='active'""", (parent,))}
        if not active:
            return
        index = TranscriptIndex(self.path.parent)
        for log in transcript_paths(app_data, parent):
            confirmed = index.query(log, candidates=active)
            if confirmed is INDEX_PENDING:
                return
            for child in confirmed or ():
                if parent_conversation(app_data, child) == parent:
                    self.close_child(child)
                    active.remove(child)
            if not active:
                return

    def settle(self, app_data, cid=None):
        if cid:
            self.reap(app_data, cid)
        with self.db() as db:
            if cid:
                db.execute("UPDATE windows SET state='incomplete' WHERE cid=? AND state='active'", (cid,))
            db.execute("UPDATE windows SET state='incomplete' WHERE state='active' AND started<?", (time.time() - MAX_WINDOW,))
            rows = db.execute("""SELECT cid, invocation, anchor FROM windows WHERE owner IS NULL
                               AND started>? ORDER BY started DESC LIMIT 64""", (time.time() - 86400,)).fetchall()
        # A native child's launch result can be written after the child starts. Reconcile by exact ID, never by time.
        pairs = [(key, owner_for(app_data, key[0], key[2], self.path.parent)) for key in rows]
        if any(owner for _, owner in pairs):
            with self.db() as db:
                for key, owner in pairs:
                    if owner:
                        self.assign(db, key, owner)

    def sessions(self):
        with self.db() as db:
            return db.execute("SELECT owner, MAX(started) FROM windows WHERE owner IS NOT NULL GROUP BY owner").fetchall()

    def turns(self, owner, selected):
        with self.db() as db:
            recent = db.execute("SELECT DISTINCT number FROM windows WHERE owner=? ORDER BY number DESC LIMIT 12", (owner,)).fetchall()
            keys = sorted(set(selected).union(n for n, in recent), reverse=True)
            if not keys:
                return []
            rows = db.execute(f"""SELECT number, role, tag, port, state, bytes FROM windows WHERE owner=?
                                AND number IN ({','.join('?' for _ in keys)})""", (owner, *keys)).fetchall()
        turns = {key: {"turn": key, "main": [], "child": []} for key in keys}
        for number, role, tag, port, state, count in rows:
            turns[number][role].append((tag, port, state, count))
        return [{"turn": number, **{role: summarize(turns[number][role]) for role in ("main", "child")}}
                for number in keys if turns[number]["main"] or turns[number]["child"]]


def summarize(rows):
    if not rows:
        return {"status": "unobserved"}
    tags = {(tag, port) for tag, port, _, _ in rows}
    if len(tags) != 1:
        return {"status": "incomplete"}
    tag = next(iter(tags))[0]
    states = {state for _, _, state, _ in rows}
    # Do not display partial estimates as the entire role's traffic.
    for state in ("overlap", "reset", "incomplete", "unavailable", "active"):
        if state in states:
            valid = [n for _, _, status, n in rows if status == "estimated"]
            return {"status": state, "tag": tag, **({"sampledBytes": sum(valid), "windows": len(valid)} if valid else {})}
    return {"status": "estimated", "tag": tag, "bytes": sum(n for _, _, _, n in rows), "windows": len(rows)}
