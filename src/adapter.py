"""Replace historical Gemini media parts and hydrate current MCP tool results."""

import hashlib
import json
import re
from dataclasses import dataclass

from conversation_names import prompt_fingerprint
from protocol import ATTACHMENT_ID, FETCH_TOOL, LIST_TOOL, MARKER, tool_matches


INLINE = ("inlineData", "inline_data")
FILE = ("fileData", "file_data")
RESPONSE = ("functionResponse", "function_response")
CALL = ("functionCall", "function_call")
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
INTERNAL_USER_TAGS = ("<SYSTEM_MESSAGE>", "<ADDITIONAL_METADATA>", "<USER_SETTINGS_CHANGE>")
SCOPE_FIELDS = ("conversationId", "conversation_id", "trajectoryId", "trajectory_id", "sessionId", "session_id")
SCOPE_HEADERS = ("x-conversation-id", "x-trajectory-id", "x-session-id")


def obj(value):
    return value if isinstance(value, dict) else {}


def items(value):
    return value if isinstance(value, list) else []


def get(value, *keys):
    return next((value[key] for key in keys if key in value), None)


def request_payload(body):
    nested = obj(body.get("request"))
    if isinstance(nested.get("contents"), list):
        return nested
    return body


def scope_of(body, request, headers):
    for key in SCOPE_FIELDS:
        for source in (request, body):
            value = source.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{8,200}", value):
                return hashlib.sha256(value.encode()).hexdigest(), key
    for key in SCOPE_HEADERS:
        value = headers.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{8,200}", value):
            return hashlib.sha256(value.encode()).hexdigest(), key
    return None, None


def declared_tools(request):
    return {
        declaration["name"]
        for tool in items(request.get("tools"))
        for declaration in items(get(obj(tool), "functionDeclarations", "function_declarations"))
        if isinstance(obj(declaration).get("name"), str)
    }


def is_user_prompt(content):
    parts = list(map(obj, items(content.get("parts"))))
    return (
        content.get("role") == "user"
        and not any(get(part, *RESPONSE) is not None for part in parts)
        and any(is_visible_text(part) or media_part(part) for part in parts)
    )


def prompt_text(part):
    value = part.get("text")
    return value.strip() if isinstance(value, str) and not part.get("thought") else ""


def is_visible_text(part):
    text = prompt_text(part)
    return bool(text and not text.startswith(INTERNAL_USER_TAGS))


def media_only(content):
    return is_user_prompt(content) and not any(is_visible_text(obj(part)) for part in items(content.get("parts")))


def prompt_turns(contents):
    """Map each visible user turn to a stable key and its first content index."""
    prompts = [index for index, content in enumerate(contents) if is_user_prompt(obj(content))]
    tagged = [index for index in prompts if any(prompt_text(obj(part)).startswith("<USER_REQUEST>")
              for part in items(obj(contents[index]).get("parts")))]
    # Desktop compaction can prepend an untagged synthetic user summary.
    anchors = tagged or prompts
    turns = {}
    for index in anchors:
        if not tagged and media_only(obj(contents[index])) and index + 1 in anchors:
            continue
        start = index
        while start > 0 and media_only(obj(contents[start - 1])):
            start -= 1
        text = next((prompt_text(obj(part)) for part in items(obj(contents[index]).get("parts"))
                     if is_visible_text(obj(part))), "")
        # Desktop timestamps survive compaction. For generic envelopes use the
        # position so identical prompts in the same history remain separate.
        desktop_key = prompt_fingerprint(text)
        if desktop_key:
            key = desktop_key
        else:
            basis = text or json.dumps(items(obj(contents[index]).get("parts")), sort_keys=True, ensure_ascii=True)
            key = hashlib.sha256(f"{len(turns)}\0{basis}".encode("utf-8", "surrogatepass")).hexdigest()
        turns[start] = (key, text)
    return turns


def media_mime(value):
    mime = get(value, "mimeType", "mime_type")
    if not isinstance(mime, str) or not re.fullmatch(r"[a-zA-Z0-9.+_-]+/[a-zA-Z0-9.+_-]+", mime):
        return None
    canonical = mime.lower()
    if canonical in OFFICE_MIME or canonical.startswith(MEDIA_PREFIXES):
        return mime
    return None


def media_part(part):
    for key in (*INLINE, *FILE):
        payload = obj(part.get(key))
        mime = media_mime(payload)
        data = payload.get("data") if key in INLINE else get(payload, "fileUri", "file_uri")
        if mime and isinstance(data, str) and data:
            return key, payload, mime
    return None


def name_of(value):
    if not isinstance(value, str):
        return ""
    basename = re.split(r"[\\/]", value)[-1]
    return re.sub(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]", "", basename).strip()[:160]


def filenames(call):
    args = obj(call.get("args"))
    file_keys = {"path", "filepath", "absolutepath", "targetfile", "file", "filename"}
    return [name for key, value in args.items() if key.lower().replace("_", "") in file_keys and (name := name_of(value))]


def placeholder(attachment_id, name, mime):
    label = f" | {json.dumps(name, ensure_ascii=False)}" if name else ""
    return f"[Archived attachment {attachment_id}{label} | {mime}. Fetch only if needed: {FETCH_TOOL}.]"


def markers_in(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from markers_in(child)
    elif isinstance(value, list):
        for child in value:
            yield from markers_in(child)
    elif isinstance(value, str):
        for match in re.finditer(re.escape(MARKER), value):
            tail = value[match.end():].splitlines()[0] if value[match.end():] else ""
            if len(tail) <= 1000:
                try:
                    yield obj(json.loads(tail))
                except ValueError:
                    continue


@dataclass
class Report:
    state: str = "unrecognized"
    scope_field: str | None = None
    seen: int = 0
    omitted: int = 0
    restored: int = 0
    listed: int = 0
    deferred: str | None = None
    scope: str | None = None
    turn_key: str | None = None

    @property
    def changed(self):
        return bool(self.omitted or self.restored or self.listed or self.state == "tool-result")


def rewrite(body, headers, archive, report):
    request = request_payload(body)
    contents = items(request.get("contents"))
    if not contents:
        return
    scope, report.scope_field = scope_of(body, request, headers)
    if scope is None:
        report.state = "missing-conversation-id"
        return
    declarations = declared_tools(request)
    if not any(tool_matches(name, FETCH_TOOL) for name in declarations):
        report.state = "plugin-tool-unavailable"
        return

    report.state = "active"
    turns = prompt_turns(contents)
    current_turn = max(turns, default=0)
    turn_key, text = turns.get(current_turn, (None, ""))
    # Keep the envelope key used by desktop conversation matching.
    prompt_count = sum(is_user_prompt(obj(c)) for c in contents[:current_turn])
    turn = hashlib.sha256(f"{prompt_count}\0{text}".encode("utf-8", "surrogatepass")).hexdigest() if turn_key else None
    # The App may place a synthetic Model message after a tool result. A fetch
    # becomes historical after Gemini produces its next real function call or reply.
    recent_model = max((index for index, content in enumerate(contents) if is_model_output(obj(content))), default=-1)
    with archive.connect() as db:
        report.scope = scope
        report.turn_key = turn_key
        mode = archive.observe(db, scope, turn, prompt_fingerprint(text) if turn else None,
                               active_turn_key=turn_key)
        archive.observe_turns(db, scope, (key for key, _ in turns.values()))
        calls = {}
        active_turn = None
        for index, raw_content in enumerate(contents):
            if index in turns:
                active_turn = turns[index][0]
                calls.clear()
            historical = index < current_turn and mode != "paused"
            process_content(obj(raw_content), historical, index > recent_model, calls, declarations,
                            archive, db, scope, report, active_turn)
        if mode == "paused" and report.state == "active":
            report.state = "paused"


def is_model_output(content):
    if content.get("role") != "model":
        return False
    for part in map(obj, items(content.get("parts"))):
        text = prompt_text(part)
        if get(part, *CALL) is not None or media_part(part) or (text and not text.startswith(INTERNAL_USER_TAGS)):
            return True
    return False


def process_content(content, historical, fresh_result, calls, declarations, archive, db, scope, report, turn):
    parts = items(content.get("parts"))
    related_names = []
    active_call_names = []
    for index, raw_part in enumerate(parts):
        part = obj(raw_part)
        call = obj(get(part, *CALL))
        if isinstance(call.get("name"), str):
            queue = calls.setdefault(call["name"], [])
            queue.append(call)
            active_call_names.extend(filenames(call))
        response = obj(get(part, *RESPONSE))
        name = response.get("name")
        if isinstance(name, str):
            queue = calls.get(name, [])
            matched = queue[0] if queue else None
            if "id" in response:
                matched = next((entry for entry in queue if entry.get("id") == response["id"]), None)
            if matched is not None:
                queue.remove(matched)
                related_names = filenames(matched)
            else:
                related_names = []
            if name in declarations and matched is not None and resolve_tool(response, matched, historical, fresh_result, part, archive, db, scope, report, turn):
                continue
            archive_response(response, historical, related_names, archive, db, scope, report, turn)
        media = media_part(part)
        if media is not None and content.get("role") in {"user", "model", "tool", "function"}:
            found_name = (related_names or active_call_names or [""])[0]
            omit = historical and not get(part, "thoughtSignature", "thought_signature")
            source = READ if related_names or content.get("role") in {"tool", "function"} else (
                GENERATED if content.get("role") == "model" else UPLOADED)
            entry = archive_media(part, media, found_name, archive, db, scope, report, turn, source)
            if omit:
                parts[index] = {"text": entry}
                report.omitted += 1


def archive_media(part, media, found_name, archive, db, scope, report, turn, source):
    _, payload, mime = media
    name = name_of(get(payload, "displayName", "display_name")) or found_name
    attachment_id, name = archive.save(db, scope, name, mime, part)
    report.seen += 1
    archive.record_occurrence(db, scope, turn, attachment_id, source)
    return placeholder(attachment_id, name, mime)


def archive_response(response, historical, related_names, archive, db, scope, report, turn):
    name = (related_names or [""])[0]
    refs = {}
    kept = []
    unreferenced = []
    for part in items(response.get("parts")):
        media = media_part(obj(part))
        if media is None:
            kept.append(part)
            continue
        entry = archive_media(part, media, name, archive, db, scope, report, turn, READ)
        if not historical:
            continue
        display = get(media[1], "displayName", "display_name")
        if isinstance(display, str) and display:
            refs[display] = entry
        else:
            unreferenced.append(entry)
        report.omitted += 1
    if historical and (refs or unreferenced):
        if kept:
            response["parts"] = kept
        else:
            response.pop("parts", None)
        previous = obj(response.get("response"))
        response["response"] = replace_refs(previous, refs)
        # Also expose parts that have a displayName but were never referenced in response JSON.
        response["response"]["_agyCamArchivedAttachments"] = [*refs.values(), *unreferenced]
    if "response" in response:
        response["response"] = walk_response(response["response"], historical, name, archive, db, scope, report, turn)


def replace_refs(value, refs):
    if isinstance(value, dict):
        ref = value.get("$ref")
        if len(value) == 1 and isinstance(ref, str) and ref in refs:
            return refs[ref]
        return {key: replace_refs(child, refs) for key, child in value.items()}
    if isinstance(value, list):
        return [replace_refs(child, refs) for child in value]
    return value


def walk_response(value, historical, name, archive, db, scope, report, turn):
    if isinstance(value, list):
        return [walk_response(child, historical, name, archive, db, scope, report, turn) for child in value]
    if not isinstance(value, dict):
        return value
    media = media_part(value)
    if media is not None:
        entry = archive_media(value, media, name, archive, db, scope, report, turn, READ)
        if historical:
            report.omitted += 1
            return {key: child for key, child in value.items() if key != media[0]} | {"archivedAttachment": entry}
        return value
    return {key: walk_response(child, historical, name, archive, db, scope, report, turn) for key, child in value.items()}


def resolve_tool(response, call, historical, fresh, wrapper, archive, db, scope, report, turn):
    name = response["name"]
    if not (tool_matches(name, FETCH_TOOL) or tool_matches(name, LIST_TOOL)):
        return False
    markers = list(markers_in(response.get("response")))
    if len(markers) != 1:
        return False
    args = obj(call.get("args"))
    marker = markers[0]
    report.state = "tool-result"
    if tool_matches(name, LIST_TOOL):
        query, offset = args.get("query", ""), args.get("offset", 0)
        if isinstance(query, str) and len(query) <= 120 and type(offset) is int and 0 <= offset <= 1_000_000 and marker == {"query": query, "offset": offset}:
            response["response"] = archive.list(db, scope, query, offset) if fresh and not historical else {"note": "Attachment catalogue requested earlier. List again for current results."}
            report.listed += int(fresh and not historical)
        else:
            response["response"] = {"error": "Attachment catalogue arguments do not match the validated tool result."}
        return True

    attachment_id = args.get("id")
    if not (isinstance(attachment_id, str) and ATTACHMENT_ID.fullmatch(attachment_id) and marker == {"id": attachment_id}):
        response["response"] = {"error": "Attachment ID does not match the validated tool result."}
        return True
    if historical or not fresh:
        report.deferred = "older-turn" if historical else "after-model-output"
        response["response"] = {"id": attachment_id, "note": "Original retrieved earlier. Fetch this ID again only if needed."}
        return True
    record = archive.load(db, scope, attachment_id)
    if record is None:
        response["response"] = {"id": attachment_id, "error": "This attachment is unavailable in the current conversation."}
        return True
    label, mime, stored = record
    media = media_part(stored)
    if media is None:
        response["response"] = {"id": attachment_id, "error": "Stored attachment is invalid."}
        return True
    key, payload, _ = media
    snake = "function_response" in wrapper
    display = label or attachment_id
    blob = {"mime_type" if snake else "mimeType": mime, "display_name" if snake else "displayName": display}
    if key in INLINE:
        blob["data"] = payload["data"]
        original = {"inline_data" if snake else "inlineData": blob}
    else:
        blob["file_uri" if snake else "fileUri"] = get(payload, "fileUri", "file_uri")
        original = {"file_data" if snake else "fileData": blob}
    video = get(stored, "videoMetadata", "video_metadata")
    if isinstance(video, dict):
        original["video_metadata" if snake else "videoMetadata"] = video
    response["parts"] = [original]
    response["response"] = {"id": attachment_id, "name": label, "mimeType": mime, "original": {"$ref": display}}
    report.restored += 1
    archive.record_occurrence(db, scope, turn, attachment_id, FETCHED)
    return True

