"""Index successful Antigravity file reads without copying the source files."""

import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from archive import Archive
from conversation_names import UUID, fingerprint, prompt_fingerprint
from media import local_path, source_file
from native_state import data_dir


MAX_EVENT = 2 * 1024 * 1024
MAX_TRANSCRIPT = 8 * 1024 * 1024


def transcript_turn(value, conversation_id, step):
    path = local_path(value)
    if (path is not None and path.name in ("transcript_full.jsonl", "transcript.jsonl")
            and tuple(name.lower() for name in path.parts[-5:-1]) ==
            ("brain", conversation_id, ".system_generated", "logs")):
        turn = read_transcript_turn(path, step)
        if turn is not None:
            return turn
    # Some desktop builds omit the hook transcript or report it before it is available.
    folder = Path.home() / ".gemini" / "antigravity" / "brain" / conversation_id / ".system_generated" / "logs"
    for filename in ("transcript_full.jsonl", "transcript.jsonl"):
        candidate = folder / filename
        if candidate != path:
            turn = read_transcript_turn(candidate, step)
            if turn is not None:
                return turn
    return None


def read_transcript_turn(path, step):
    try:
        if path.resolve(strict=True) != path or path.stat().st_size > MAX_TRANSCRIPT:
            return None
        with path.open("rb") as stream:
            raw = stream.read(MAX_TRANSCRIPT + 1)
    except OSError:
        return None
    if len(raw) > MAX_TRANSCRIPT:
        return None
    number, anchor, key = 0, -1, None
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(row, dict) or row.get("type") != "USER_INPUT":
            continue
        index = row.get("step_index")
        if type(index) is not int or index < 0 or index >= step:
            continue
        if index <= anchor:
            return None
        number, anchor = number + 1, index
        text = row.get("content")
        key = prompt_fingerprint(text.strip()) if isinstance(text, str) else None
    return (number, key or fingerprint(f"native-turn:{anchor}")) if number else None


def record_event(event, directory=None):
    if not isinstance(event, dict) or event.get("error") not in (None, ""):
        return
    conversation_id, step = event.get("conversationId"), event.get("stepIdx")
    call = event.get("toolCall")
    if (not isinstance(conversation_id, str) or not UUID.fullmatch(conversation_id)
            or type(step) is not int or not 0 <= step < 2**31 or not isinstance(call, dict)
            or call.get("name") not in ("view_file", "read_file")):
        return
    args = call.get("args")
    if isinstance(args, str):
        args = json.loads(args)
    if not isinstance(args, dict):
        return
    source = source_file(args.get("AbsolutePath", args.get("Path", args.get("path"))))
    if source is None:
        return
    path, mime = source
    conversation_id = conversation_id.lower()
    turn = transcript_turn(event.get("transcriptPath"), conversation_id, step)
    archive = Archive(directory or data_dir())
    archive.record_local_read(conversation_id, path, mime, turn, int(time.time()))


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_EVENT + 1)
        if len(raw) <= MAX_EVENT:
            record_event(json.loads(raw))
    except (OSError, ValueError, RuntimeError, RecursionError, sqlite3.Error):
        # Indexing must not interrupt Antigravity's native tool flow.
        print("Context Attachment Manager could not index this read.", file=sys.stderr)
    print("{}")


if __name__ == "__main__":
    main()
