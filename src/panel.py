"""Local tools for the Antigravity MCP Apps panel."""

import base64
import hmac
import os
import re
import secrets
import sqlite3
import stat
from pathlib import Path

from archive import PAGE_SIZE
from conversation_names import UUID, desktop_names, fingerprint
from desktop_selection import desktop_selection, public_selection
from legacy_timeline import backfill
from media import EXTENSIONS, INLINE, MIME_BY_EXTENSION, local_path, media_part


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
            with archive.connect() as db:
                sessions = archive.sessions(db)
            _, current_id = desktop_selection()
            names, links = desktop_names(sessions, current_id=current_id)
            if links:
                with archive.connect() as db:
                    archive.link_conversations(db, links)
            for session in sessions:
                conversation_id = session.get("conversationId") or links.get(session["scope"])
                if isinstance(conversation_id, str) and UUID.fullmatch(conversation_id):
                    session["desktopKey"] = fingerprint(conversation_id.lower())
                for key in ("turn", "promptFingerprint", "conversationId"):
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
            linked = db.execute("SELECT conversation_id FROM session_names WHERE scope=?", (scope,)).fetchone()
            if linked and offset == 0:
                backfill(archive, db, scope, linked[0])
            return archive.entries(db, scope, query, kind, offset) | {"pageSize": PAGE_SIZE}
    if action == "session":
        label = args.get("label")
        if isinstance(label, str) and len(label) <= 100:
            db.execute("UPDATE sessions SET label=? WHERE scope=?", (label.strip(), scope))
            return {"ok": True}
    attachment_id = args.get("id")
    if isinstance(attachment_id, str) and ATTACHMENT_ID.fullmatch(attachment_id):
        local = db.execute("""
            SELECT l.path, a.mime FROM local_files l JOIN attachments a USING (scope, id)
            WHERE l.scope=? AND l.id=?
        """, (scope, attachment_id)).fetchone()
        if local is not None and action in ("open", "thumbnail"):
            return local_preview(*local, action)
        if action == "open":
            return open_attachment(archive, db, scope, attachment_id)
        if action == "thumbnail":
            info = db.execute("""
                SELECT mime, file_size, length(payload) FROM attachments WHERE scope=? AND id=?
            """, (scope, attachment_id)).fetchone()
            if info is None:
                return {"available": False}
            mime, file_size, payload_size = info
            too_large = (file_size > MAX_THUMBNAIL if file_size is not None
                         else payload_size > MAX_THUMBNAIL * 4 // 3 + 4096)
            if mime.lower() not in THUMBNAIL_MIMES or too_large:
                return {"available": False}
            record = archive.load(db, scope, attachment_id)
            return thumbnail(record) if record else {"available": False}
    return {"error": "素材操作无效，或素材已不在该会话的档案中。"}


def local_preview(value, mime, action):
    path = local_path(value)
    # Accept the same canonical path and an explicitly supported association.
    if (path is None or MIME_BY_EXTENSION.get(path.suffix.lower()) != mime
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


def thumbnail(record):
    _, mime, part = record
    media = media_part(part)
    if media is None or media[0] not in INLINE:
        return {"available": False}
    data = media[1]["data"]
    if len(data) > MAX_THUMBNAIL * 4 // 3 + 4 or not re.fullmatch(r"[a-zA-Z0-9+/]+={0,2}", data):
        return {"available": False}
    return {"available": True, "mimeType": mime.lower(), "data": data}


def open_attachment(archive, db, scope, attachment_id):
    row = db.execute("SELECT mime, file_digest FROM attachments WHERE scope=? AND id=?",
                     (scope, attachment_id)).fetchone()
    if row is None:
        return {"error": "找不到该素材。"}
    mime, digest = row
    if mime.lower() not in EXTENSIONS:
        return {"error": "此文件格式暂不支持从素材库打开。"}
    if not hasattr(os, "startfile"):
        return {"error": "此功能需要 Windows 默认应用。"}
    try:
        if digest is None:
            digest = archive.migrate_original(db, scope, attachment_id)
            if digest is None:
                return {"error": "没有可在 Windows 打开的本地原件。素材可能是服务端引用或已损坏。"}
            db.commit()
        path = archive.original_path(mime, digest)
        if not path.is_file():
            return {"error": "本地归档文件已丢失。"}
        path.chmod(stat.S_IREAD)
        os.startfile(str(path))
    except (OSError, ValueError):
        return {"error": "Windows 无法打开此素材。请检查归档文件和默认应用。"}
    return {"ok": True}
