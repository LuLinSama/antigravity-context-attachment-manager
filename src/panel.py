"""Local tools for the Antigravity MCP Apps panel."""

import base64
import hmac
import os
import re
import secrets
import sqlite3
from pathlib import Path

from archive import PAGE_SIZE, reconcile_reads
from conversation_names import UUID, desktop_names, fingerprint
from desktop_selection import desktop_selection, public_selection
from media_delegation import desktop_data
from media import DELEGATED_READ, MEDIA_TYPES, local_path
from usage import Usage, settings


URI = "ui://antigravity-context-attachment-manager/attachments.html"
HTML = Path(__file__).resolve().parents[1] / "ui" / "attachments.html"
RESOURCE = {"uri": URI, "name": "上下文素材", "mimeType": "text/html;profile=mcp-app"}
OPEN_PANEL = "cam_open_panel"
MAX_THUMBNAIL = 512 * 1024
CAPABILITY = secrets.token_hex(32)
ATTACHMENT_ID = re.compile(r"CAM-[0-9a-f]{32}")
THUMBNAIL_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/avif"}

PANEL_TOOLS = [
    {
        "name": OPEN_PANEL,
        "description": "打开上下文素材管理面板。",
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string"}, "capability": {"type": "string"},
            "scope": {"type": "string"}, "id": {"type": "string"},
            "query": {"type": "string"}, "kind": {"type": "string"},
            "offset": {"type": "integer"}, "label": {"type": "string"},
            "turns": {"type": "array", "items": {"type": "integer"}, "maxItems": 20},
        }, "additionalProperties": False},
        "_meta": {
            "ui": {"resourceUri": URI, "visibility": ["app"]},
            "com.google.antigravity/ui": {"entrypoints": [
                {"type": "aux_pane", "title": "上下文素材"},
                {"type": "global", "title": "上下文素材"},
            ]},
        },
    },
]


def contents():
    html = HTML.read_text(encoding="utf-8").replace("__CAM_PANEL_CAPABILITY__", CAPABILITY)
    return {"contents": [{**RESOURCE, "text": html}]}


def authorized(args):
    token = args.get("capability")
    return (isinstance(token, str) and len(token) == len(CAPABILITY) and token.isascii()
            and hmac.compare_digest(token, CAPABILITY))


def result(payload):
    return {"content": [{"type": "text", "text": payload.get("error", "Context Attachment Manager")}],
            "structuredContent": payload, "isError": "error" in payload}


def run(archive, args):
    try:
        action = args.get("action")
        if action == "selection":
            return public_selection(*desktop_selection())
        if action == "sessions":
            _, current_id = desktop_selection()
            app_data = desktop_data()
            if current_id and UUID.fullmatch(current_id):
                reconcile_reads(archive, app_data, current_id.lower())
            usage = Usage(archive.path.parent)
            if usage.path.is_file():
                usage.settle(app_data)
                if current_id and UUID.fullmatch(current_id):
                    usage.reap(app_data, current_id.lower())
                timings = usage.sessions()
            else:
                timings = []
            with archive.connect() as db:
                for owner, started in timings:
                    archive.touch(db, owner, int(started))
                sessions = archive.sessions(db)
            names, links = desktop_names(sessions, current_id=current_id)
            if links:
                with archive.connect() as db:
                    archive.link_conversations(db, links)
            for session in sessions:
                conversation_id = session.get("conversationId") or links.get(session["scope"])
                if isinstance(conversation_id, str) and UUID.fullmatch(conversation_id):
                    session["desktopKey"] = fingerprint(conversation_id.lower())
                for key in ("promptFingerprint", "conversationId"):
                    session.pop(key, None)
                session.update(names.get(session["scope"], {}))
            return {"online": True, "sessions": sessions}
        with archive.connect() as db:
            return apply_action(archive, db, action, args)
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        return {"error": "无法读取本机素材库，请重试。"}


def apply_action(archive, db, action, args):
    scope = args.get("scope")
    if not isinstance(scope, str) or not re.fullmatch(r"[0-9a-f]{64}", scope):
        return {"error": "请选择一个会话。"}
    session = db.execute("SELECT 1 FROM sessions WHERE scope=?", (scope,)).fetchone()
    if session is None:
        return {"error": "这个会话已无法读取，请刷新。"}
    if action == "entries":
        query, kind, offset = args.get("query", ""), args.get("kind", ""), args.get("offset", 0)
        if isinstance(query, str) and len(query) <= 120 and isinstance(kind, str) and type(offset) is int and 0 <= offset <= 1_000_000:
            return archive.entries(db, scope, query, kind, offset) | {"pageSize": PAGE_SIZE}
    if action == "traffic":
        owner = db.execute("SELECT conversation_id FROM session_names WHERE scope=?", (scope,)).fetchone()
        usage = Usage(archive.path.parent)
        config = settings(archive.path.parent)
        selected = args.get("turns", [])
        if not isinstance(selected, list) or len(selected) > PAGE_SIZE or any(type(n) is not int or n < 1 or n >= 2**31 for n in selected):
            return {"error": "无效的轮次。"}
        return {"turns": usage.turns(owner[0], selected) if owner and usage.path.is_file() else [],
                "enabled": bool(config and config.enabled), "tag": config.tag if config and config.enabled else None}
    if action == "session":
        label = args.get("label")
        if isinstance(label, str) and len(label) <= 100:
            db.execute("UPDATE sessions SET label=? WHERE scope=?", (label.strip(), scope))
            return {"ok": True}
    attachment_id = args.get("id")
    if isinstance(attachment_id, str) and ATTACHMENT_ID.fullmatch(attachment_id):
        local = db.execute("""
            SELECT l.path, a.mime FROM local_files l JOIN attachments a USING (scope, id)
            WHERE l.scope=? AND l.id=? AND EXISTS (
                SELECT 1 FROM turn_attachments u WHERE u.scope=l.scope AND u.id=l.id AND (u.sources & ?)<>0
            )
        """, (scope, attachment_id, DELEGATED_READ)).fetchone()
        if local is not None and action in ("open", "thumbnail"):
            return local_preview(*local, action)
    return {"error": "素材操作无效，或素材已不在该会话的档案中。"}


def local_preview(value, mime, action):
    path = local_path(value)
    # Accept the same canonical path and an explicitly supported association.
    if (path is None or MEDIA_TYPES.get(path.suffix.lower()) != mime
            or not path.is_file() or path.resolve() != path):
        return {"available": False} if action == "thumbnail" else {"error": "所引用的原文件已移动或删除。"}
    if action == "open":
        if not hasattr(os, "startfile"):
            return {"error": "此功能需要 Windows 默认应用。"}
        os.startfile(str(path))
        return {"ok": True}
    if mime not in THUMBNAIL_MIMES or path.stat().st_size > MAX_THUMBNAIL:
        return {"available": False}
    with path.open("rb") as stream:
        data = stream.read(MAX_THUMBNAIL + 1)
    if not data or len(data) > MAX_THUMBNAIL:
        return {"available": False}
    return {"available": True, "mimeType": mime, "data": base64.b64encode(data).decode("ascii")}
