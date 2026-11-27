"""Prevent main-context media reads, and allow one native read per verified CAM child."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hook_event import FILE_TOOLS, conversation_id, file_argument, run_hook, step_index, tool_call
from media import classify, local_path
from media_delegation import CAM_MEDIA, conversation_identity, desktop_data, user_turn
from native_state import data_dir
from usage import Usage


ALLOW = {"decision": "allow"}
CANNOT_VERIFY = {
    "decision": "deny",
    "reason": "CAM: Cannot verify a new CAM Media child for this read. Stop this media task; do not delegate again or bypass the file tool.",
}
DELEGATE = {
    "decision": "deny",
    "reason": "CAM: Do not read this media in the main context. Call native invoke_subagent with TypeName cam-media, one local absolute path and the question. Use only the returned text. If unavailable, report the limitation; do not bypass the file tool.",
}
ONE_READ = {
    "decision": "deny",
    "reason": "CAM Media may read only one local media file per child. Return your text findings or report the limitation. Do not invoke a subagent or retry in this context.",
}
NESTED_TOOLS = ("invoke_subagent", "define_subagent", "manage_subagents")


def guard(event, directory=None, app_data=None):
    name, args = tool_call(event)
    if name not in (*FILE_TOOLS, *NESTED_TOOLS):
        return ALLOW
    value = file_argument(args)
    is_media = name in FILE_TOOLS and classify(value, event.get("workspacePaths"))
    current = conversation_id(event)
    app_data = app_data or desktop_data()
    identity = conversation_identity(app_data, current) if current else None
    # The native catalogue can briefly lag behind a newly launched child's first tool call.
    if is_media and current and identity is None:
        for _ in range(2):
            time.sleep(.06)
            identity = conversation_identity(app_data, current)
            if identity is not None:
                break
    if identity and identity[0] == CAM_MEDIA:
        step = event.get("stepIdx")
        if (name not in FILE_TOOLS or not identity[1] or not is_media or not local_path(value)
                or not step_index(step) or user_turn(app_data, current, step + 1)):
            return ONE_READ
        usage = Usage(directory or data_dir())
        return ALLOW if usage.reserve(current, step, value) else ONE_READ
    if not is_media:
        return ALLOW
    if current and ((identity is not None and not identity[1]) or
                    (identity is None and user_turn(app_data, current))):
        return DELEGATE
    return CANNOT_VERIFY


if __name__ == "__main__":
    run_hook(guard, ALLOW, CANNOT_VERIFY)
