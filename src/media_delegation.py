"""Locate a CAM Media launch from Antigravity's local transcript and catalogue."""

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from conversation_names import UUID


LOG_NAMES = ("transcript_full.jsonl", "transcript.jsonl")
CAM_MEDIA = "cam-media"
NATIVE_PROMPT = re.compile(r"<ADDITIONAL_METADATA>\s*The current local time is: \d{4}-\d\d-\d\dT")
LAUNCH_RESULT = re.compile(r"^Created the following subagent:[ \t]*\r?\n", re.M)


def transcript_paths(app_data, conversation_id):
    folder = app_data / "brain" / conversation_id / ".system_generated" / "logs"
    return (folder / name for name in LOG_NAMES)


def conversation_identity(app_data, child):
    path = app_data / "conversation_summaries.db"
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
            result = db.execute("""
                SELECT agent_name, parent_conversation_id FROM conversation_summaries
                WHERE conversation_id=?
            """, (child,)).fetchone()
    except sqlite3.Error:
        return None
    if result is None:
        return None
    agent, parent = result
    if parent not in (None, "") and (not isinstance(parent, str) or not UUID.fullmatch(parent) or parent.lower() == child):
        return None
    parent = parent.lower() if parent else None
    return agent, parent


def parent_conversation(app_data, child):
    identity = conversation_identity(app_data, child)
    return identity[1] if identity and identity[0] == CAM_MEDIA else None


def launch_ids(content):
    if not isinstance(content, str):
        return
    for match in LAUNCH_RESULT.finditer(content):
        try:
            record, _ = json.JSONDecoder().raw_decode(content[match.end():].lstrip())
        except (ValueError, RecursionError):
            continue
        if isinstance(record, dict):
            child = record.get("conversationId")
            if isinstance(child, str) and UUID.fullmatch(child):
                yield child.lower()


def is_cam_launch(call):
    if not isinstance(call, dict) or call.get("name") != "invoke_subagent":
        return False
    args = call.get("args")
    if not isinstance(args, dict):
        return False
    specs = args.get("Subagents", [args])
    if isinstance(specs, str):  # Compact desktop transcripts encode argument values as JSON.
        try:
            specs = json.loads(specs)
        except (ValueError, RecursionError):
            return False
    return isinstance(specs, list) and any(
        isinstance(spec, dict) and spec.get("TypeName") in (CAM_MEDIA, '"cam-media"')
        for spec in specs
    )


def desktop_data():
    return Path.home() / ".gemini" / "antigravity"


def kill_targets(args):
    if not isinstance(args, dict) or args.get("Action") not in ("kill", '"kill"'):
        return ()
    ids = args.get("ConversationIds")
    if isinstance(ids, str):
        try:
            ids = json.loads(ids)
        except (ValueError, RecursionError):
            return ()
    if not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str) or not UUID.fullmatch(ids[0]):
        return ()
    return (ids[0].lower(),)
