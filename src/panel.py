"""Local tools for the Antigravity MCP Apps panel."""

import base64
import binascii
import os
import re
import sqlite3
import tempfile
from pathlib import Path

from adapter import INLINE, media_part
from archive import PAGE_SIZE
from conversation_names import UUID, desktop_names, fingerprint
from desktop_selection import desktop_selection, public_selection
from native_state import data_dir, read_state
from legacy_timeline import backfill
from protocol import ATTACHMENT_ID


URI = "ui://antigravity-context-attachment-manager/attachments.html"
HTML = Path(__file__).resolve().parents[1] / "ui" / "attachments.html"
RESOURCE = {"uri": URI, "name": "上下文素材", "mimeType": "text/html;profile=mcp-app"}
OPEN_PANEL = "cam_open_panel"
MAX_THUMBNAIL = 512 * 1024
MODES = {"auto", "paused"}
# Choose the Windows association from the archived MIME, never from a user filename.
EXTENSIONS = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
    "image/bmp": ".bmp", "image/tiff": ".tif", "image/heic": ".heic", "image/avif": ".avif",
    "video/mp4": ".mp4", "video/mpeg": ".mpeg", "video/webm": ".webm", "video/quicktime": ".mov",
    "video/x-matroska": ".mkv", "video/x-msvideo": ".avi", "video/x-ms-wmv": ".wmv",
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/ogg": ".ogg", "audio/aac": ".aac", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
    "audio/flac": ".flac", "audio/x-flac": ".flac", "audio/webm": ".webm",
    "application/pdf": ".pdf", "application/msword": ".doc", "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt", "application/rtf": ".rtf", "text/rtf": ".rtf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.oasis.opendocument.text": ".odt", "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    "text/plain": ".txt", "text/markdown": ".md", "text/csv": ".csv",
}
THUMBNAIL_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/avif"}

PANEL_TOOLS = [
    {
        "name": OPEN_PANEL,
        "description": "打开上下文素材管理面板。",
        "inputSchema": {"type": "object", "properties": {}},
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
    endpoint = os.environ.get("AGY_CAM_PANEL_URL") or read_state(data_dir() / "native-panel.json").get("url", "")
    match = re.fullmatch(r"(http://127\.0\.0\.1:\d{1,5})/panel-[A-Za-z0-9_-]{32}", endpoint) if isinstance(endpoint, str) else None
    metadata = {"csp": {"connectDomains": [match[1]]}} if match else {}
    html = HTML.read_text(encoding="utf-8").replace("__CAM_PANEL_URL__", endpoint if match else "")
    return {"contents": [{**RESOURCE, "text": html, "_meta": {"ui": metadata}}]}


def result(payload):
    return {"content": [{"type": "text", "text": payload.get("error", "Context Attachment Manager")}],
            "structuredContent": payload, "isError": "error" in payload}


def run(archive, args, routed=True):
    if archive is None:
        return {"online": False, "sessions": []}
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
            return {"online": True, "routed": routed, "sessions": sessions}
        with archive.connect() as db:
            return apply_action(archive, db, action, args)
    except (OSError, sqlite3.Error, ValueError):
        return {"error": "无法读取本机素材库，请重试。"}


def apply_action(archive, db, action, args):
    scope = args.get("scope")
    if not isinstance(scope, str) or not re.fullmatch(r"[0-9a-f]{64}", scope):
        return {"error": "请选择一个会话。"}
    session = db.execute("SELECT mode, label FROM sessions WHERE scope=?", (scope,)).fetchone()
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
        mode, label = args.get("mode", session[0]), args.get("label", session[1])
        if isinstance(mode, str) and mode in MODES and isinstance(label, str) and len(label) <= 100:
            archive.set_session(db, scope, mode, label.strip())
            return {"ok": True}
    attachment_id = args.get("id")
    if isinstance(attachment_id, str) and ATTACHMENT_ID.fullmatch(attachment_id):
        if action in {"open", "thumbnail"}:
            if action == "thumbnail":
                info = db.execute("SELECT mime, length(payload) FROM attachments WHERE scope=? AND id=?",
                                  (scope, attachment_id)).fetchone()
                if info is None or info[0].lower() not in THUMBNAIL_MIMES or info[1] > MAX_THUMBNAIL * 4 // 3 + 4096:
                    return {"available": False}
            record = archive.load(db, scope, attachment_id)
            if record is None:
                return {"error": "找不到该素材。"}
            if action == "open":
                return open_attachment(archive.path.parent, attachment_id, record)
            return thumbnail(record)
    return {"error": "素材操作无效，或素材已不在该会话的档案中。"}


def thumbnail(record):
    _, mime, part = record
    media = media_part(part)
    if media is None or media[0] not in INLINE:
        return {"available": False}
    data = media[1]["data"]
    if len(data) > MAX_THUMBNAIL * 4 // 3 + 4 or not re.fullmatch(r"[a-zA-Z0-9+/]+={0,2}", data):
        return {"available": False}
    return {"available": True, "mimeType": mime.lower(), "data": data}


def open_attachment(directory, attachment_id, record):
    name, mime, part = record
    media = media_part(part)
    if media is None or media[0] not in INLINE:
        return {"error": "归档只保存了服务端引用，没有可在 Windows 打开的本地原件。"}
    extension = EXTENSIONS.get(mime.lower())
    if not extension:
        return {"error": "此文件格式暂不支持从素材库打开。"}
    if not hasattr(os, "startfile"):
        return {"error": "此功能需要 Windows 默认应用。"}
    try:
        original = base64.b64decode(media[1]["data"], validate=True)
    except (binascii.Error, ValueError):
        return {"error": "素材原件已损坏，无法打开。"}

    base = re.split(r"[\\/]", name)[-1]
    stem = Path(base).stem
    stem = re.sub(r'[^\w .()-]', '_', stem).strip(' .')[:70]
    cache = directory.resolve() / "preview-cache"
    try:
        cache.mkdir(exist_ok=True)
        if cache.resolve() != cache:
            return {"error": "无法使用当前预览缓存目录。"}
        path = cache / f"{attachment_id}-{stem or 'attachment'}{extension}"
        # Replace atomically so another panel click cannot open a partial file.
        if path.is_symlink():
            return {"error": "无法使用当前预览缓存文件。"}
        if not path.is_file() or path.read_bytes() != original:
            with tempfile.NamedTemporaryFile(dir=cache, delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    stream.write(original)
                    stream.close()
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
        os.startfile(str(path))
    except OSError:
        return {"error": "Windows 无法打开此素材。请检查默认应用或预览缓存。"}
    return {"ok": True}
