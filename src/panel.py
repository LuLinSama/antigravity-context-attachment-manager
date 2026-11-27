"""Local tools for the Antigravity MCP Apps panel."""

import base64
import hmac
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path

from archive import PAGE_SIZE, reconcile_reads
from conversation_names import UUID, desktop_names, fingerprint
from desktop_selection import desktop_selection, public_selection
from media_delegation import CAM_MEDIA, conversation_identity, desktop_data
from media import DELEGATED_READ, MEDIA_TYPES, local_path, source_file
from usage import MODES, Usage, settings


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
            "mode": {"type": "string", "enum": list(MODES)}, "path": {"type": "string", "maxLength": 32767},
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
        usage = Usage(archive.path.parent)
        if action == "set_mode":
            mode = args.get("mode")
            if mode not in MODES:
                return {"error": "无效的读取模式。"}
            usage.set_mode(mode)
            return usage.policy()
        if action in ("policy", "grant", "revoke"):
            return media_policy(usage, action, args)
        if action == "sessions":
            _, current_id = desktop_selection()
            app_data = desktop_data()
            if current_id and UUID.fullmatch(current_id):
                reconcile_reads(archive, app_data, current_id.lower())
            if usage.path.is_file():
                usage.settle(app_data)
                if current_id and UUID.fullmatch(current_id):
                    usage.reap(app_data, current_id.lower())
                timings = usage.sessions()
            else:
                timings = []
            with archive.connect() as db:
                for owner, started in timings:
                    archive.touch(db, owner, started)
                sessions = archive.sessions(db, {owner for owner, _ in timings})
            names = desktop_names(sessions)
            for session in sessions:
                del session["conversationId"]
                session.update(names.get(session["scope"], {}))
            return {"online": True, "sessions": sessions}
        with archive.connect() as db:
            return apply_action(archive, db, action, args)
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        return {"error": "无法读取本机素材库，请重试。"}


def media_policy(usage, action, args):
    status, current = desktop_selection()
    owner = current.lower() if status == "active" and current and UUID.fullmatch(current) else None
    if action == "policy":
        return usage.policy(owner) | {"scope": fingerprint(owner) if owner else None}
    if not owner or args.get("scope") != fingerprint(owner):
        return {"error": "请先切换到要临时放行的 Antigravity 主会话，再试一次。"}
    if action == "revoke":
        usage.revoke(owner)
        return usage.policy(owner) | {"scope": fingerprint(owner)}
    identity = conversation_identity(desktop_data(), owner)
    if identity is None or identity[0] == CAM_MEDIA or identity[1]:
        return {"error": "还无法确认当前是主会话。请先发送一条纯文字消息，再试一次。"}
    source = source_file(args.get("path"))
    if source is None:
        return {"error": "请填写仍存在且受支持的本机媒体原件的绝对路径。"}
    expires = usage.grant(owner, source[0])
    if expires is None:
        return {"error": "请先切换到省流模式。"}
    return {"mode": "save", "grantUntil": expires, "scope": fingerprint(owner)}


def archived_file(db, scope, attachment_id):
    return db.execute("""
        SELECT l.path, a.mime FROM local_files l JOIN attachments a USING (scope, id)
        WHERE l.scope=? AND l.id=? AND EXISTS (
            SELECT 1 FROM turn_attachments u WHERE u.scope=l.scope AND u.id=l.id AND (u.sources & ?)<>0
        )
    """, (scope, attachment_id, DELEGATED_READ)).fetchone()


def apply_action(archive, db, action, args):
    scope = args.get("scope")
    if not isinstance(scope, str) or not re.fullmatch(r"[0-9a-f]{64}", scope):
        return {"error": "请选择一个会话。"}
    if action == "delete_session":
        db.execute("BEGIN IMMEDIATE")
    owner = archive.owner(db, scope)
    if owner is None:
        return {"error": "这个会话已无法读取，请刷新。"}
    if action == "delete_session":
        deleted_at = time.time()
        archive.clear(db, scope, owner, deleted_at)
        usage = Usage(archive.path.parent)
        if usage.path.is_file():
            usage.clear(owner, deleted_at)
        return {"ok": True}
    if action == "entries":
        query, kind, offset = args.get("query", ""), args.get("kind", ""), args.get("offset", 0)
        if isinstance(query, str) and len(query) <= 120 and isinstance(kind, str) and type(offset) is int and 0 <= offset <= 1_000_000:
            return archive.entries(db, scope, query, kind, offset) | {"pageSize": PAGE_SIZE}
    if action == "traffic":
        usage = Usage(archive.path.parent)
        config = settings(archive.path.parent)
        selected = args.get("turns", [])
        if not isinstance(selected, list) or len(selected) > PAGE_SIZE or any(type(n) is not int or n < 1 or n >= 2**31 for n in selected):
            return {"error": "无效的轮次。"}
        return {"turns": usage.turns(owner, selected) if usage.path.is_file() else [],
                "enabled": bool(config and config.enabled)}
    if action == "session":
        label = args.get("label")
        if isinstance(label, str) and len(label) <= 100:
            db.execute("UPDATE sessions SET label=? WHERE scope=?", (label.strip(), scope))
            return {"ok": True}
    attachment_id = args.get("id")
    if isinstance(attachment_id, str) and ATTACHMENT_ID.fullmatch(attachment_id):
        local = archived_file(db, scope, attachment_id)
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
