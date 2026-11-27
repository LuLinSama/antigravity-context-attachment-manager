"""Sample local counters at native model-invocation boundaries, never in the request path."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from archive import Archive, reconcile_reads
from hook_event import conversation_id, run_hook, step_index, tool_call
from media_delegation import desktop_data, kill_targets, parent_conversation, user_turn
from native_state import data_dir
from usage import Usage, owner_for, settings


def lifecycle(event, phase, directory=None, app_data=None):
    current = conversation_id(event)
    if not current:
        return None
    directory, app_data = directory or data_dir(), app_data or desktop_data()
    usage = Usage(directory)
    if phase == "kill":
        name, args = tool_call(event)
        if name == "manage_subagents" and event.get("error") in (None, ""):
            for child in kill_targets(args):
                if parent_conversation(app_data, child) == current:
                    usage.close_child(child)
        return None
    if phase == "stop":
        if event.get("fullyIdle") is True:
            parent = parent_conversation(app_data, current)
            if parent:
                usage.close_child(current)
            if usage.path.is_file():
                usage.settle(app_data, current)
            if (directory / "attachments.sqlite3").is_file():
                reconcile_reads(Archive(directory), app_data, parent or current)
        return {"decision": "stop"}
    invocation, anchor = event.get("invocationNum"), event.get("initialNumSteps")
    if not (step_index(invocation) and step_index(anchor)):
        return None
    if phase == "pre" and parent_conversation(app_data, current) and (
        user_turn(app_data, current, anchor) or (usage.path.is_file() and usage.closed(current))
    ):
        return {"terminationBehavior": "terminate", "injectSteps": [{"errorMessage": {
            "userMessage": "CAM Media is one-shot. Request a fresh media inspection in the main conversation.",
        }}]}
    config = settings(directory)
    if phase == "pre" and (not config or not config.enabled):
        return None
    if phase == "post" and (not config or not config.enabled) and not (directory / "usage.sqlite3").is_file():
        return None
    key = (current, invocation, anchor)
    owner = owner_for(app_data, current, anchor)
    if phase == "pre":
        if owner and owner[3] == "main" and usage.path.is_file():
            usage.reap(app_data, current)
        usage.begin(key, owner, config)
    elif phase == "post":
        usage.end(key, owner, config)
    return None


if __name__ == "__main__":
    phase = sys.argv[1]
    run_hook(lambda event: lifecycle(event, phase), {"decision": "stop"} if phase == "stop" else {})
