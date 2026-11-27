"""Read titles and project names from the local Antigravity desktop catalogue."""

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlsplit

from native_state import read_state


UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
TRANSCRIPT_WINDOW = 256 * 1024
DESKTOP_MESSAGE = re.compile(r"<ADDITIONAL_METADATA>\s*The current local time is: \d{4}-\d\d-\d\dT")
SUMMARY_FIELDS = "conversation_id, title, project_id, workspace_uris"


def fingerprint(value):
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def prompt_fingerprint(text):
    # The desktop's timestamp disambiguates repeated user instructions across chats.
    if text.startswith("<USER_REQUEST>") and DESKTOP_MESSAGE.search(text):
        return fingerprint(text.strip())
    return None


def desktop_names(sessions, gemini=None, *, current_id=None):
    gemini = gemini or Path.home() / ".gemini"
    references = {s["scope"]: s.get("conversationId") for s in sessions if s.get("conversationId")}
    unmatched = [s for s in sessions if s["scope"] not in references]
    agy = gemini / "antigravity"
    summary = agy / "conversation_summaries.db"
    if not summary.is_file() or not sessions:
        return {}, {}
    summary_ids = set(references.values())
    if current_id and unmatched:
        summary_ids.add(current_id)
    summaries = read_summaries(summary, summary_ids, bool(unmatched))
    fresh = match_sessions(unmatched, summaries, agy)
    references.update(fresh)
    names = {}
    projects = {}
    for scope, conversation_id in references.items():
        if conversation_id not in summaries:
            continue
        title, project_id, workspace_uris = summaries[conversation_id]
        if project_id not in projects:
            projects[project_id] = project_name(gemini, project_id)
        project = projects[project_id] or workspace_name(workspace_uris)
        if title or project:
            names[scope] = {"title": title or "", "project": project}
    return names, fresh


def read_summaries(path, conversation_ids, include_recent):
    uri = path.resolve().as_uri() + "?mode=ro"
    for address in (uri, uri + "&immutable=1"):
        try:
            with closing(sqlite3.connect(address, uri=True, timeout=.25)) as db:
                rows = []
                if include_recent:
                    rows.extend(db.execute(f"""
                        SELECT {SUMMARY_FIELDS} FROM conversation_summaries
                        ORDER BY last_user_input_time DESC LIMIT 48
                    """))
                rows.extend(row for conversation_id in conversation_ids if UUID.fullmatch(conversation_id)
                            for row in db.execute(f"SELECT {SUMMARY_FIELDS} FROM conversation_summaries WHERE conversation_id=?", (conversation_id,)))
                return {row[0]: row[1:] for row in rows if isinstance(row[0], str) and UUID.fullmatch(row[0])}
        except sqlite3.Error:
            continue
    return {}


def match_sessions(sessions, summaries, app_data):
    if not sessions:
        return {}
    direct = {fingerprint(conversation_id): conversation_id for conversation_id in summaries}
    matches = {s["scope"]: direct[s["scope"]] for s in sessions if s["scope"] in direct}
    sessions = [s for s in sessions if s["scope"] not in matches]
    turn_keys = {s.get("turn") for s in sessions if s.get("turn")}
    prompt_keys = {s.get("promptFingerprint") for s in sessions if s.get("promptFingerprint")}
    if not turn_keys and not prompt_keys:
        return matches

    by_turn = {}
    by_prompt = {}
    for conversation_id in summaries:
        transcript = app_data / "brain" / conversation_id / ".system_generated" / "logs" / "transcript_full.jsonl"
        try:
            stat = transcript.stat()
        except OSError:
            continue
        for digest, turn in transcript_keys(transcript, stat.st_mtime_ns, stat.st_size):
            if digest in prompt_keys:
                by_prompt.setdefault(digest, set()).add(conversation_id)
            if turn in turn_keys:
                by_turn.setdefault(turn, set()).add(conversation_id)
    for session in sessions:
        candidates = by_prompt.get(session.get("promptFingerprint"), set()) | by_turn.get(session.get("turn"), set())
        if len(candidates) == 1:
            matches[session["scope"]] = next(iter(candidates))
    return matches


@lru_cache(maxsize=64)
def transcript_keys(path, _modified, _size):
    return tuple((digest, fingerprint(f"{count}\0{value}") if count is not None else None)
                 for count, value in user_messages(path) if (digest := prompt_fingerprint(value)))


def user_messages(path):
    try:
        with path.open("rb") as transcript:
            size = transcript.seek(0, 2)
            start = max(0, size - TRANSCRIPT_WINDOW)
            transcript.seek(start)
            if start:
                transcript.readline()
            lines = transcript.read(TRANSCRIPT_WINDOW).splitlines()
    except OSError:
        return
    count = 0
    for line in lines:
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(row, dict) and row.get("type") == "USER_INPUT" and isinstance(row.get("content"), str):
            yield count if not start else None, row["content"].strip()
            count += 1


def project_name(gemini, project_id):
    if not isinstance(project_id, str) or not UUID.fullmatch(project_id):
        return ""
    value = read_state(gemini / "config" / "projects" / f"{project_id}.json").get("name")
    return value.strip() if isinstance(value, str) else ""


def workspace_name(value):
    try:
        workspaces = json.loads(value or "[]")
    except (TypeError, ValueError):
        return ""
    if not isinstance(workspaces, list):
        return ""
    for workspace in workspaces:
        if isinstance(workspace, str):
            try:
                name = unquote(urlsplit(workspace).path.rstrip("/").rsplit("/", 1)[-1])
            except ValueError:
                continue
            if name:
                return name
    return ""
