"""Read the current desktop route through Antigravity's existing local target list."""

import http.client
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from conversation_names import UUID, fingerprint


MAX_TARGETS_BYTES = 256 * 1024
LIBRARY_ROUTE = "/view/mcp-app/antigravity-context-attachment-manager_media/cam_open_panel"


def desktop_target_file():
    if sys.platform == "win32":
        profile = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Antigravity"
        return profile / "DevToolsActivePort"
    return None


def selection_from_targets(targets):
    if not isinstance(targets, list):
        return "unavailable", None
    routes = []
    for target in targets:
        if not isinstance(target, dict) or target.get("type") != "page" or not isinstance(target.get("url"), str):
            continue
        try:
            url = urlsplit(target["url"])
        except ValueError:
            continue
        if url.scheme == "https" and re.fullmatch(r"(?:127\.0\.0\.1|localhost):[0-9]{1,5}", url.netloc):
            routes.append(url.path)
    # The target list lacks focus information. A second window makes selection ambiguous.
    if len(routes) != 1:
        return "unavailable", None
    route, = routes
    chat = re.fullmatch(r"/c/([^/]+)/?", route)
    if chat and UUID.fullmatch(chat[1]):
        return "active", chat[1]
    return ("library" if unquote(route) == LIBRARY_ROUTE else "none"), None


def public_selection(status, conversation_id):
    return {"status": status, "key": fingerprint(conversation_id.lower()) if conversation_id else None}


def desktop_selection():
    port_file = desktop_target_file()
    if port_file is None:
        return "unavailable", None
    try:
        with port_file.open("rb") as stream:
            port = stream.readline(16).strip()
        if not re.fullmatch(rb"[0-9]{1,5}", port) or not 0 < int(port) <= 65535:
            return "unavailable", None
        connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=0.4)
        try:
            connection.request("GET", "/json/list")
            response = connection.getresponse()
            if response.status != 200:
                return "unavailable", None
            payload = response.read(MAX_TARGETS_BYTES + 1)
            if len(payload) > MAX_TARGETS_BYTES:
                return "unavailable", None
            return selection_from_targets(json.loads(payload))
        finally:
            connection.close()
    except (OSError, ValueError, RecursionError, http.client.HTTPException):
        return "unavailable", None
