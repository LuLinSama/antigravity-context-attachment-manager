"""Read titles and project names for known native Antigravity conversations."""

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote, urlsplit

from native_state import read_state


UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)


def fingerprint(value):
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def desktop_names(sessions, gemini=None):
    if not sessions:
        return {}
    gemini = gemini or Path.home() / ".gemini"
    summary = gemini / "antigravity" / "conversation_summaries.db"
    references = {s["scope"]: s["conversationId"] for s in sessions}
    summaries = read_summaries(summary, set(references.values()))
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
    return names


def read_summaries(path, conversation_ids):
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
            summaries = {}
            for cid in conversation_ids:
                row = db.execute("""
                    SELECT title, project_id, workspace_uris FROM conversation_summaries WHERE conversation_id=?
                """, (cid,)).fetchone()
                if row is not None:
                    summaries[cid] = row
            return summaries
    except sqlite3.Error:
        return {}


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
