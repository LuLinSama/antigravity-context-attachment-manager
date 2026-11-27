"""Native hook parsing shared by the media guard and the lifecycle hooks."""

import json
import sqlite3
import sys

from conversation_names import UUID


FILE_TOOLS = ("view_file", "read_file")
MAX_EVENT = 2 * 1024 * 1024


def conversation_id(event):
    value = event.get("conversationId")
    return value.lower() if isinstance(value, str) and UUID.fullmatch(value) else None


def step_index(value):
    return type(value) is int and 0 <= value < 2**31


def tool_call(event):
    call = event.get("toolCall")
    if not isinstance(call, dict):
        return None, {}
    args = call.get("args")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, RecursionError):
            args = None
    return call.get("name"), args if isinstance(args, dict) else {}


def file_arguments(args):
    return tuple(args[key] for key in ("AbsolutePath", "Path", "path") if key in args)


def file_argument(args):
    return next(iter(file_arguments(args)), None)


def run_hook(handler, default=None, failure=None):
    result = default or {}
    try:
        raw = sys.stdin.buffer.read(MAX_EVENT + 1)
        event = json.loads(raw) if len(raw) <= MAX_EVENT else None
        if isinstance(event, dict):
            result = handler(event) or result
        elif failure is not None:
            result = failure
    except (OSError, ValueError, RuntimeError, RecursionError, sqlite3.Error):
        if failure is not None:
            result = failure
        print("CAM: hook could not process this event.", file=sys.stderr)
    print(json.dumps(result, ensure_ascii=True))
