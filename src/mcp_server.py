"""Stdio MCP Apps server for the local attachment library."""

import json
import sqlite3
import sys
from functools import lru_cache
from pathlib import Path

import panel
from archive import Archive
from native_state import data_dir


VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
MANIFEST = json.loads((Path(__file__).resolve().parents[1] / "plugin.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def archive():
    return Archive(data_dir())


def tool_result(name, args):
    # Antigravity enables the registered app entrypoint for iframe calls.
    if name == panel.OPEN_PANEL and not args:
        return panel.result({"title": "上下文素材"})
    if name == panel.OPEN_PANEL and panel.authorized(args):
        try:
            return panel.result(panel.run(archive(), args))
        except (OSError, sqlite3.Error):
            return panel.result({"error": "无法打开本机素材库。"})
    return panel.result({"error": "此操作只能从上下文素材面板调用。"})


def dispatch(message):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    if "id" not in message:
        return None
    envelope = {"jsonrpc": "2.0", "id": message["id"]}
    method, params = message.get("method"), message.get("params", {})
    if not isinstance(params, dict):
        return {**envelope, "error": {"code": -32602, "message": "Invalid parameters"}}
    if method == "initialize":
        version = params.get("protocolVersion")
        result = {
            "protocolVersion": version if isinstance(version, str) and version in VERSIONS else "2025-11-25",
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": MANIFEST["name"], "version": MANIFEST["version"]},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": panel.PANEL_TOOLS}
    elif method == "resources/list":
        result = {"resources": [panel.RESOURCE]}
    elif method == "resources/templates/list":
        result = {"resourceTemplates": []}
    elif method == "resources/read" and params.get("uri") == panel.URI:
        result = panel.contents()
    elif method == "tools/call" and isinstance(params.get("arguments", {}), dict):
        result = tool_result(params.get("name"), params.get("arguments", {}))
    else:
        return {**envelope, "error": {"code": -32601, "message": "Method unavailable"}}
    return {**envelope, "result": result}


def main():
    for line in sys.stdin.buffer:
        try:
            response = dispatch(json.loads(line))
        except (ValueError, RecursionError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
