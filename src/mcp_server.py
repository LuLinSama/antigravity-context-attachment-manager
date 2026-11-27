"""MCP tool results carry retrieval instructions for the local request proxy."""

import json
import sys

import panel
from protocol import ATTACHMENT_ID, FETCH_TOOL, LIST_TOOL, MARKER


TOOLS = [
    {
        "name": FETCH_TOOL,
        "description": "Retrieve exactly one historical attachment by its CAM ID when its original media is needed.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "pattern": f"^{ATTACHMENT_ID.pattern}$"}},
            "required": ["id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": LIST_TOOL,
        "description": "List archived attachment IDs and filenames in this conversation. Returns metadata only. Useful after compaction.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 120},
                "offset": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]
VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}


def tool_result(name, args):
    if name == panel.OPEN_PANEL:
        return panel.result({"title": "上下文素材"})
    if name == FETCH_TOOL:
        attachment_id = args.get("id")
        valid = isinstance(attachment_id, str) and ATTACHMENT_ID.fullmatch(attachment_id)
        if valid:
            return marker_result({"id": attachment_id})
        message = "Provide a complete CAM attachment ID from this conversation."
    elif name == LIST_TOOL:
        query, offset = args.get("query", ""), args.get("offset", 0)
        if isinstance(query, str) and len(query) <= 120 and type(offset) is int and 0 <= offset <= 1_000_000:
            return marker_result({"query": query, "offset": offset})
        message = "Provide a filename query of at most 120 characters and a valid offset."
    else:
        message = "Unknown attachment tool."
    return {"content": [{"type": "text", "text": message}], "isError": True}


def marker_result(args):
    message = MARKER + json.dumps(args, ensure_ascii=True, separators=(",", ":"))
    message += "\nAwaiting the local attachment proxy. If this text is visible, check the connection in the Context Attachments panel."
    return {"content": [{"type": "text", "text": message}]}


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
            "serverInfo": {"name": "antigravity-context-attachment-manager", "version": "0.3.3"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS + panel.PANEL_TOOLS}
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
