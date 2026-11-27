"""Index verified successful CAM Media reads by the exact parent launch turn."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from archive import Archive, reconcile_reads
from hook_event import FILE_TOOLS, conversation_id, file_argument, run_hook, step_index, tool_call
from media import source_file
from media_delegation import desktop_data, parent_conversation
from native_state import data_dir
from transcript_index import user_turn
from usage import Usage


def record_event(event, directory=None, app_data=None):
    updated = time.time()
    if event.get("error") not in (None, ""):
        return
    child, step = conversation_id(event), event.get("stepIdx")
    tool, args = tool_call(event)
    if not child or not step_index(step) or tool not in FILE_TOOLS:
        return
    value = file_argument(args)
    source = source_file(value)
    if source is None:
        return
    app_data = app_data or desktop_data()
    directory = directory or data_dir()
    if not (directory / "usage.sqlite3").is_file():
        return
    parent = parent_conversation(app_data, child)
    if not parent or user_turn(app_data, child, directory=directory):
        return
    if not Usage(directory).reserved(child, step, value):
        return
    path, mime = source
    archive = Archive(directory)
    archive.queue_read(child, parent, path, mime, updated)
    reconcile_reads(archive, app_data, parent)


if __name__ == "__main__":
    run_hook(record_event)
