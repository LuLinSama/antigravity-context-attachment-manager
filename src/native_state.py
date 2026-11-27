"""Per-user settings shared by the native sidecar and MCP Apps resource."""

import json
import os
import sys
import tempfile
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


def write_state(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def exclude_loopback():
    entries = [value.strip() for value in os.environ.get("NO_PROXY", "").split(",") if value.strip()]
    for host in ("localhost", "127.0.0.1", "::1"):
        if host not in entries:
            entries.append(host)
    os.environ["NO_PROXY"] = ",".join(entries)
