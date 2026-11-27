"""Cross-process index of native JSONL facts; never stores transcript text or media."""

import hashlib
import json
import os
import sqlite3
import stat
import time
from contextlib import closing
from dataclasses import dataclass

from conversation_names import UUID, fingerprint
from media_delegation import NATIVE_PROMPT, is_cam_launch, kill_targets, launch_ids, transcript_paths


MAX_LINE = 16 * 1024 * 1024
CHUNK = 256 * 1024
SCAN_SECONDS = .75
PROBE = 128
INDEX_PENDING = object()
SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS logs (
    log TEXT PRIMARY KEY, stamp TEXT NOT NULL, size INTEGER NOT NULL, pos INTEGER NOT NULL,
    probe BLOB NOT NULL, tail INTEGER NOT NULL, skipping INTEGER NOT NULL,
    number INTEGER NOT NULL, anchor INTEGER NOT NULL, ordered INTEGER NOT NULL,
    launch INTEGER, killing TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    log TEXT NOT NULL, step INTEGER NOT NULL, number INTEGER NOT NULL, turn_key TEXT NOT NULL,
    PRIMARY KEY(log, step)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS launches (
    log TEXT NOT NULL, child TEXT NOT NULL, step INTEGER NOT NULL, PRIMARY KEY(log, child)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS kills (
    log TEXT NOT NULL, child TEXT NOT NULL, PRIMARY KEY(log, child)
) WITHOUT ROWID;
PRAGMA user_version=1;
COMMIT;
"""


@dataclass
class State:
    number: int = 0
    anchor: int = -1
    ordered: bool = True
    launch: int | None = None
    killing: str | None = None

    def consume(self, db, log, row):
        kind, step = row.get("type"), row.get("step_index")
        valid_step = type(step) is int and 0 <= step < 2**31
        if kind == "USER_INPUT":
            self.launch = self.killing = None
            if valid_step:
                if step <= self.anchor:
                    self.ordered = False
                self.number += 1
                self.anchor = step
                text = row.get("content")
                text = text.strip() if isinstance(text, str) else ""
                key = (fingerprint(text) if text.startswith("<USER_REQUEST>") and NATIVE_PROMPT.search(text)
                       else fingerprint(f"native-turn:{step}"))
                db.execute("INSERT OR IGNORE INTO turns VALUES (?, ?, ?, ?)", (log, step, self.number, key))
        if kind == "PLANNER_RESPONSE":
            self.killing = None
        if row.get("source") != "MODEL" or row.get("status") != "DONE":
            return
        if kind == "PLANNER_RESPONSE":
            tools = row.get("tool_calls")
            self.launch = step if valid_step and isinstance(tools, list) and any(map(is_cam_launch, tools)) else None
            if (isinstance(tools, list) and len(tools) == 1 and isinstance(tools[0], dict)
                    and tools[0].get("name") == "manage_subagents"):
                targets = kill_targets(tools[0].get("args"))
                self.killing = targets[0] if targets else None
        elif kind == "GENERIC":
            content = row.get("content")
            if self.launch is not None and valid_step and step > self.launch:
                db.executemany("INSERT OR IGNORE INTO launches VALUES (?, ?, ?)",
                               ((log, child, self.launch) for child in launch_ids(content)))
            if self.killing:
                expected = "Successfully killed 1 subagent(s) and their descendants."
                if isinstance(content, str) and expected in content.splitlines():
                    db.execute("INSERT OR IGNORE INTO kills VALUES (?, ?)", (log, self.killing))
                self.killing = None


def decode(raw):
    try:
        row = json.loads(raw)
        return row if isinstance(row, dict) else None
    except (ValueError, RecursionError):
        return None


def probe(stream, pos):
    """Check a bounded prefix and the last indexed boundary before trusting an append."""
    digest = hashlib.blake2s(digest_size=16)
    stream.seek(0)
    digest.update(stream.read(min(PROBE, pos)))
    stream.seek(max(0, pos - PROBE))
    digest.update(stream.read(min(PROBE, pos)))
    return digest.digest()


def signature(info):
    return f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_ctime_ns}"


def cached(db, log):
    return db.execute("""SELECT stamp,size,pos,probe,tail,skipping,number,anchor,ordered,launch,killing
                         FROM logs WHERE log=?""", (log,)).fetchone()


def unchanged(record, stamp, size):
    return record is not None and record[0] == stamp and record[1] == size and (record[2] == size or record[4])


def scan(db, log, stream, stamp, size, previous):
    state, pos, skipping, tail = State(), 0, False, False
    if (previous and previous[0].split(":")[:2] == stamp.split(":")[:2]
            and (size > previous[1] or (size == previous[1] and stamp == previous[0]))
            and previous[2] <= size and probe(stream, previous[2]) == previous[3]):
        pos, skipping = previous[2], previous[5]
        state = State(*previous[6:])
    elif previous:
        for table in ("turns", "launches", "kills"):
            db.execute(f"DELETE FROM {table} WHERE log=?", (log,))
    stream.seek(pos)
    deadline = time.monotonic() + SCAN_SECONDS
    provisional = None
    while pos < size and time.monotonic() < deadline:
        if skipping:
            piece = stream.readline(min(CHUNK, size - pos))
            if not piece:
                break
            pos += len(piece)
            skipping = not piece.endswith(b"\n")
            continue
        line = stream.readline(min(MAX_LINE + 1, size - pos))
        if not line:
            break
        if len(line) > MAX_LINE:
            pos += len(line)
            skipping = not line.endswith(b"\n")
            state.launch = state.killing = None  # Never match a result across an opaque record.
            continue
        row = decode(line)
        if not line.endswith(b"\n"):
            if pos + len(line) == size:
                tail, provisional = True, row
            break  # Do not commit a partial line or a JSON value that may still change.
        pos += len(line)
        if row is not None:
            state.consume(db, log, row)
        elif line.strip():
            state.launch = state.killing = None
    db.execute("INSERT OR REPLACE INTO logs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (log, stamp, size, pos, probe(stream, pos), tail, skipping, state.number,
                state.anchor, state.ordered, state.launch, state.killing))
    return state, pos == size or tail, provisional


def lookup(db, log, state, step, child, candidates):
    if candidates is not None:
        return {cid for cid in candidates if db.execute("SELECT 1 FROM kills WHERE log=? AND child=?", (log, cid)).fetchone()}
    if child is not None:
        launch = db.execute("SELECT step FROM launches WHERE log=? AND child=?", (log, child)).fetchone()
        if not launch:
            return None
        step = launch[0]
    if not state.ordered or not state.number:
        return None
    return db.execute("""SELECT number, turn_key FROM turns WHERE log=? AND step<?
                         ORDER BY step DESC LIMIT 1""", (log, step if step is not None else 2**31)).fetchone()


class TranscriptIndex:
    def __init__(self, directory):
        self.path = directory / "transcripts.sqlite3"

    def query(self, path, step=None, child=None, candidates=None):
        if not UUID.fullmatch(path.parents[2].name):
            return None
        try:
            if path.resolve(strict=True) != path or not path.is_file():
                return None
            with path.open("rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or not info.st_size:
                    return None
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(self.path, timeout=.35)) as db:
                    if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                        db.executescript(SCHEMA)
                    log = str(path)
                    db.execute("BEGIN")
                    previous = cached(db, log)
                    stamp, size = signature(info), info.st_size
                    if unchanged(previous, stamp, size) and not previous[4]:
                        return lookup(db, log, State(*previous[6:]), step, child, candidates)
                    db.rollback()
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        # The writer may have finished while this Hook waited for the lock.
                        info = os.fstat(stream.fileno())
                        stamp, size = signature(info), info.st_size
                        previous = cached(db, log)
                        if unchanged(previous, stamp, size):
                            state, complete, provisional = State(*previous[6:]), True, None
                            if previous[4]:
                                stream.seek(previous[2])
                                provisional = decode(stream.read(size - previous[2]))
                        else:
                            state, complete, provisional = scan(db, log, stream, stamp, size, previous)
                        after = os.fstat(stream.fileno())
                        if signature(after) != stamp:
                            # An active writer can append during a cold scan. Keep a verified
                            # checkpoint, but do not answer from that now-incomplete snapshot.
                            record = cached(db, log)
                            if after.st_size <= size or probe(stream, record[2]) != record[3]:
                                db.rollback()
                            return INDEX_PENDING
                        if not complete:
                            return INDEX_PENDING  # A later Hook continues from the checkpoint.
                        if provisional is None or provisional.get("type") not in ("USER_INPUT", "GENERIC"):
                            return lookup(db, log, state, step, child, candidates)
                        db.execute("SAVEPOINT provisional")
                        state.consume(db, log, provisional)
                        answer = lookup(db, log, state, step, child, candidates)
                        db.execute("ROLLBACK TO provisional")
                        db.execute("RELEASE provisional")
                        return answer
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or "locked" in str(exc):
                return INDEX_PENDING
            return None
        except (OSError, RuntimeError, sqlite3.Error):
            return None


def user_turn(app_data, conversation, step=None, *, directory):
    index = TranscriptIndex(directory)
    for log in transcript_paths(app_data, conversation):
        turn = index.query(log, step=step)
        if turn is INDEX_PENDING:
            return None  # Do not substitute a compact log while the preferred log catches up.
        if turn is not None:
            return turn
    return None


def delegated_turn(app_data, parent, child, *, directory):
    index = TranscriptIndex(directory)
    for log in transcript_paths(app_data, parent):
        turn = index.query(log, child=child)
        if turn is INDEX_PENDING:
            return None  # Do not substitute a compact log while the preferred log catches up.
        if turn is not None:
            return turn
    return None
