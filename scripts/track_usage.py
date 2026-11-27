"""Sample local counters at native model-invocation boundaries, never in the request path."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hook_event import conversation_id, run_hook, step_index, tool_call
from media_delegation import CAM_MEDIA, conversation_identity, desktop_data, kill_targets, parent_conversation
from native_state import data_dir
from transcript_index import user_turn
from usage import Usage, owner_for, settings


MODE_MESSAGE = {
    "save": "CAM policy update: Save mode. For each necessary media inspection, use a fresh cam-media subagent instead of reading directly. Reuse existing text findings when sufficient.",
    "native": "CAM policy update: Native mode. In the main conversation, read user-authorized local media with the native file reader when needed. Do not delegate to CAM Media merely to enforce isolation.",
    "grant": "CAM policy update: Save mode with one temporary exception. If the user asks to directly read the specified absolute media path, try the native reader once. The guard enforces the exact path, expiry, and single use. Delegate other media as usual; do not retry a denied direct read.",
}


def notice(mode):
    if mode:
        return {"injectSteps": [{"systemMessage": {"systemMessage": MODE_MESSAGE[mode]}}]}


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
                from archive import Archive, reconcile_reads

                reconcile_reads(Archive(directory), app_data, parent or current)
        return {"decision": "stop"}
    invocation, anchor = event.get("invocationNum"), event.get("initialNumSteps")
    if not (step_index(invocation) and step_index(anchor)):
        return None
    key = (current, invocation, anchor)
    if phase == "post":
        config = settings(directory)
        if usage.path.is_file() or (config and config.enabled):
            usage.end(key, None, config,
                      resolve=lambda: owner_for(app_data, current, anchor, directory))
        return None
    if phase != "pre":
        return None
    identity = conversation_identity(app_data, current)
    if identity and identity[0] == CAM_MEDIA and (
        not identity[1] or user_turn(app_data, current, anchor, directory=directory)
        or not usage.allow_child_invocation(current, invocation, anchor)
    ):
        return {"terminationBehavior": "terminate", "injectSteps": [{"errorMessage": {
            "userMessage": "CAM Media can read once and make one final text response. Request a fresh media inspection in the main conversation if needed.",
        }}]}
    change = usage.policy_notice(current) if identity is not None and not identity[1] and identity[0] != CAM_MEDIA else None
    config = settings(directory)
    if config and config.enabled:
        owner = owner_for(app_data, current, anchor, directory, identity)
        if owner and owner[3] == "main" and usage.path.is_file():
            usage.reap(app_data, current)
        usage.begin(key, owner, config)
    return notice(change)


if __name__ == "__main__":
    phase = sys.argv[1]
    run_hook(lambda event: lifecycle(event, phase), {"decision": "stop"} if phase == "stop" else {})
