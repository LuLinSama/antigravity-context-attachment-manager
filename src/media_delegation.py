"""Locate a CAM Media launch from Antigravity's local transcript and catalogue."""

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from conversation_names import UUID, fingerprint, prompt_fingerprint


MAX_TRANSCRIPT = 8 * 1024 * 1024
LOG_NAMES = ("transcript_full.jsonl", "transcript.jsonl")
CAM_MEDIA = "cam-media"
LAUNCH_RESULT = re.compile(r"^Created the following subagent:[ \t]*\r?\n", re.M)


def transcript_rows(path):
    try:
        if path.resolve(strict=True) != path or path.stat().st_size > MAX_TRANSCRIPT:
            return
        with path.open("rb") as stream:
            raw = stream.read(MAX_TRANSCRIPT + 1)
    except OSError:
        return
    if len(raw) > MAX_TRANSCRIPT:
        return
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(row, dict):
            yield row


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
    parent = parent.lower() if isinstance(parent, str) and UUID.fullmatch(parent) and parent.lower() != child else None
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


def launch_step(rows, child):
    """Find the invocation that created this exact child; never infer by time."""
    launch = None
    for row in rows:
        index = row.get("step_index")
        if type(index) is not int or index < 0:
            continue
        if row.get("type") == "USER_INPUT":
            launch = None
        if row.get("source") != "MODEL" or row.get("status") != "DONE":
            continue
        if row.get("type") == "PLANNER_RESPONSE":
            tools = row.get("tool_calls")
            launch = index if isinstance(tools, list) and any(map(is_cam_launch, tools)) else None
        elif row.get("type") == "GENERIC" and launch is not None and index > launch:
            if child in launch_ids(row.get("content")):
                return launch
    return None


def desktop_data():
    return Path.home() / ".gemini" / "antigravity"



def turn_before(rows, step=None):
    number, anchor, key = 0, -1, None
    for row in rows:
        if row.get("type") != "USER_INPUT":
            continue
        index = row.get("step_index")
        if type(index) is not int or index < 0 or (step is not None and index >= step):
            continue
        if index <= anchor:
            return None
        number, anchor = number + 1, index
        text = row.get("content")
        key = prompt_fingerprint(text.strip()) if isinstance(text, str) else None
    return (number, key or fingerprint(f"native-turn:{anchor}")) if number else None


def user_turn(app_data, conversation, step=None):
    for log in transcript_paths(app_data, conversation):
        turn = turn_before(transcript_rows(log), step)
        if turn is not None:
            return turn
    return None


def delegated_turn(app_data, parent, child):
    for log in transcript_paths(app_data, parent):
        rows = list(transcript_rows(log))
        launch = launch_step(rows, child)
        if launch is not None:
            turn = turn_before(rows, launch)
            if turn is not None:
                return turn
    return None


def confirmed_kills(rows):
    """Recover exact native kills when a forced termination emits no child Stop hook."""
    targets = ()
    for row in rows:
        if row.get("type") in ("PLANNER_RESPONSE", "USER_INPUT"):
            targets = ()
        if row.get("source") != "MODEL" or row.get("status") != "DONE":
            continue
        if row.get("type") == "PLANNER_RESPONSE":
            calls = row.get("tool_calls")
            if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
                continue
            if calls[0].get("name") == "manage_subagents":
                targets = kill_targets(calls[0].get("args"))
        elif row.get("type") == "GENERIC" and targets:
            content = row.get("content")
            if isinstance(content, str) and f"Successfully killed {len(targets)} subagent(s) and their descendants." in content.splitlines():
                yield from targets
            targets = ()


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
