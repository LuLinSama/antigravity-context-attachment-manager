"""Backfill older archives using exact matches in Antigravity's local transcript."""

import base64
import binascii
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlsplit

from adapter import INLINE, READ, media_part
from conversation_names import UUID, prompt_fingerprint


MAX_TRANSCRIPT = 8 * 1024 * 1024
MAX_LEGACY_PAYLOAD = 24 * 1024 * 1024


@lru_cache(maxsize=64)
def transcript_media(path, _mtime, size):
    if size > MAX_TRANSCRIPT:
        return (), ()
    turns, files = [], []
    current = None
    try:
        with path.open("rb") as stream:
            contents = stream.read(MAX_TRANSCRIPT + 1)
    except OSError:
        return (), ()
    if len(contents) > MAX_TRANSCRIPT:
        return (), ()
    for line in contents.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(row, dict):
            continue
        if row.get("type") == "USER_INPUT":
            text = row.get("content")
            current = prompt_fingerprint(text.strip()) if isinstance(text, str) else None
            turns.append(current)
        # Native read_file emits this marker for a successfully read binary.
        text = row.get("content")
        media = row.get("media")
        index = row.get("step_index")
        if (not current or row.get("status") != "DONE" or not isinstance(text, str)
                or not re.search(r"(?m)^Showing binary file of type ", text)
                or not isinstance(media, list) or type(index) is not int or index < 0):
            continue
        for position, item in enumerate(media):
            if not isinstance(item, dict):
                continue
            uri, mime = item.get("uri"), item.get("mime_type")
            if not isinstance(uri, str) or not isinstance(mime, str):
                continue
            try:
                url = urlsplit(uri)
                name = unquote(url.path.rsplit("/", 1)[-1])
            except ValueError:
                continue
            # Ignore the URI's directory. Only inspect local, native transcript media.
            if url.scheme == "file" and not url.netloc and re.fullmatch(rf"step_{index}_media_{position}\.[A-Za-z0-9]+", name):
                files.append((current, mime.lower(), name))
    return tuple(turns), tuple(files)


def backfill(archive, db, scope, conversation_id, gemini=None):
    if not isinstance(conversation_id, str) or not UUID.fullmatch(conversation_id):
        return
    rows = db.execute("""
        SELECT a.id, lower(a.mime), COALESCE((a.file_size + 2) / 3 * 4, length(a.payload)), EXISTS (
            SELECT 1 FROM turn_attachments u WHERE u.scope=a.scope AND u.id=a.id
        ) FROM attachments a WHERE a.scope=?
    """, (scope,)).fetchall()
    if not any(not recorded for _, _, _, recorded in rows):
        return
    gemini = gemini or Path.home() / ".gemini"
    folder = gemini / "antigravity" / "brain" / conversation_id / ".system_generated" / "logs"
    transcript = folder / "transcript_full.jsonl"
    try:
        stat = transcript.stat()
    except OSError:
        return
    turns, files = transcript_media(transcript, stat.st_mtime_ns, stat.st_size)
    if not files or None in turns or len(set(turns)) != len(turns):
        return
    known = dict(db.execute("SELECT turn_key, number FROM turns WHERE scope=?", (scope,)))
    # Only seed official positions if previously recorded numbering agrees.
    if any(known.get(key, number) != number for number, key in enumerate(turns, 1)):
        return
    if any(number <= len(turns) and turns[number - 1] != key for key, number in known.items()):
        return
    needed_mimes = {mime for _, mime, size, recorded in rows if size <= MAX_LEGACY_PAYLOAD and not recorded}
    folder = folder.resolve() / "media"
    matching = {}
    for turn, mime, name in files:
        if mime not in needed_mimes:
            continue
        path = folder / name
        try:
            if path.resolve() != path or path.stat().st_size > MAX_LEGACY_PAYLOAD * 3 // 4:
                continue
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").digest() if hasattr(hashlib, "file_digest") else hashlib.sha256(stream.read()).digest()
            matching.setdefault((mime, digest), set()).add(turn)
        except OSError:
            continue
    if not matching:
        return
    originals = {}
    for attachment_id, mime, size, recorded in rows:
        if size > MAX_LEGACY_PAYLOAD or mime not in needed_mimes:
            continue
        record = archive.load(db, scope, attachment_id)
        media = media_part(record[2]) if record else None
        if not media or media[0] not in INLINE:
            continue
        try:
            digest = hashlib.sha256(base64.b64decode(media[1]["data"], validate=True)).digest()
        except (binascii.Error, ValueError):
            continue
        if (mime, digest) in matching:
            originals.setdefault((mime, digest), []).append((attachment_id, recorded))
    matches = {identity: records[0][0] for identity, records in originals.items()
               if len(records) == 1 and not records[0][1]}
    if not matches:
        return
    # Take the write lock and re-check because the relay can record concurrently.
    db.execute("BEGIN IMMEDIATE")
    if dict(db.execute("SELECT turn_key, number FROM turns WHERE scope=?", (scope,))) != known:
        return
    db.executemany("INSERT OR IGNORE INTO turns VALUES (?, ?, ?)",
                   ((scope, key, number) for number, key in enumerate(turns, 1)))
    for identity, attachment_id in matches.items():
        for turn in matching[identity]:
            archive.record_occurrence(db, scope, turn, attachment_id, READ)
