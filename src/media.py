"""Windows local paths and file formats routed to the CAM Media subagent."""

import os
from pathlib import Path


DELEGATED_READ = 16

MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".bmp": "image/bmp", ".tiff": "image/tiff", ".tif": "image/tiff",
    ".heic": "image/heic", ".heif": "image/heif", ".avif": "image/avif", ".ico": "image/x-icon",
    ".mp4": "video/mp4", ".mpeg": "video/mpeg", ".mpg": "video/mpeg", ".webm": "video/webm",
    ".mov": "video/quicktime", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
    ".wmv": "video/x-ms-wmv", ".m4v": "video/x-m4v", ".3gp": "video/3gpp",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".wave": "audio/wav", ".ogg": "audio/ogg",
    ".opus": "audio/opus", ".aac": "audio/aac", ".m4a": "audio/mp4", ".flac": "audio/flac",
    ".aiff": "audio/aiff", ".wma": "audio/x-ms-wma", ".pdf": "application/pdf",
    ".doc": "application/msword", ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint", ".rtf": "application/rtf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
}


def local_path(value):
    if not isinstance(value, str) or not value or "\0" in value or len(value) > 32767:
        return None
    path = Path(value)
    if not path.is_absolute() or (os.name == "nt" and
            (len(path.drive) != 2 or not path.drive[0].isascii() or not path.drive[0].isalpha()
             or ":" in value[2:])):
        return None
    return path


def media_type(value):
    if not isinstance(value, str) or not value or len(value) > 32767:
        return None
    return MEDIA_TYPES.get(Path(value.rstrip(" .")).suffix.lower())


def classify(value, workspace_paths=()):
    """Match the submitted extension or the resolved local target, never read file contents."""
    mime = media_type(value)
    if mime:
        return mime
    path = local_path(value)
    if path is None and isinstance(value, str) and 0 < len(value) <= 32767 and "\0" not in value:
        relative = Path(value)
        if not relative.anchor and isinstance(workspace_paths, list):
            return next((mime for folder in workspace_paths[:16]
                         if (root := local_path(folder)) and (mime := classify(str(root / relative)))), None)
    if path is not None:
        try:
            return media_type(path.resolve(strict=True).name)
        except (OSError, RuntimeError, ValueError):
            pass
    return None


def source_file(value):
    path = local_path(value)
    if path is None:
        return None
    try:
        resolved = path.resolve(strict=True)
        mime = media_type(resolved.name)
        if mime and local_path(str(resolved)) is not None and resolved.is_file():
            return resolved, mime
    except (OSError, RuntimeError, ValueError):
        pass
    return None
