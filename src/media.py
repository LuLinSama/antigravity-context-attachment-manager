"""Supported Windows associations and legacy media decoding."""

import os
import re
from pathlib import Path


INLINE = ("inlineData", "inline_data")
FILE = ("fileData", "file_data")
OFFICE_MIME = {
    "application/pdf", "application/msword", "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint", "application/rtf", "text/rtf",
    "text/plain", "text/markdown", "text/csv",
}
MEDIA_PREFIXES = (
    "image/", "audio/", "video/", "application/vnd.openxmlformats-officedocument.",
    "application/vnd.oasis.opendocument.", "application/vnd.ms-word.",
    "application/vnd.ms-excel.", "application/vnd.ms-powerpoint.",
)
UPLOADED, READ, GENERATED, FETCHED = 1, 2, 4, 8


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

MIME_BY_EXTENSION = {extension: mime for mime, extension in EXTENSIONS.items()}
MIME_BY_EXTENSION.update({".jpeg": "image/jpeg", ".tiff": "image/tiff", ".wave": "audio/wav"})


def local_path(value):
    if not isinstance(value, str) or not value or "\0" in value or len(value) > 32767:
        return None
    path = Path(value)
    # Exclude UNC, Windows device paths, alternate data streams, and shortcuts.
    if not path.is_absolute() or (os.name == "nt" and
            (len(path.drive) != 2 or not path.drive[0].isascii() or not path.drive[0].isalpha()
             or ":" in value[2:])):
        return None
    return path


def source_file(value):
    path = local_path(value)
    if path is None or path.suffix.lower() not in MIME_BY_EXTENSION:
        return None
    path = path.resolve(strict=True)
    if local_path(str(path)) is None or not path.is_file():
        return None
    mime = MIME_BY_EXTENSION.get(path.suffix.lower())
    return (path, mime) if mime else None


def media_mime(value):
    mime = value.get("mimeType", value.get("mime_type"))
    if not isinstance(mime, str) or not re.fullmatch(r"[a-zA-Z0-9.+_-]+/[a-zA-Z0-9.+_-]+", mime):
        return None
    canonical = mime.lower()
    if canonical in OFFICE_MIME or canonical.startswith(MEDIA_PREFIXES):
        return mime
    return None


def media_part(part):
    for key in (*INLINE, *FILE):
        payload = part.get(key)
        if not isinstance(payload, dict):
            continue
        mime = media_mime(payload)
        data = payload.get("data") if key in INLINE else payload.get("fileUri", payload.get("file_uri"))
        if mime and isinstance(data, str) and data:
            return key, payload, mime
    return None
