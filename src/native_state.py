"""Locate per-user archives and read local desktop metadata."""

import json
import os
import sys
from pathlib import Path


PLUGIN_NAME = "antigravity-context-attachment-manager"


def app_data():
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))


def data_dir():
    return app_data() / PLUGIN_NAME


def read_state(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
