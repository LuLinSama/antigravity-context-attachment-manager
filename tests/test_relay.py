import base64
import gzip
import hashlib
import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from adapter import FETCHED, READ, UPLOADED, Report, rewrite
from archive import Archive
from conversation_names import desktop_names, fingerprint
from desktop_selection import selection_from_targets
from legacy_timeline import backfill
from native_state import exclude_loopback
from native_startup import shortcut_listener
from mcp_server import dispatch, tool_result
from panel import OPEN_PANEL, URI, run as run_panel
from protocol import FETCH_TOOL, LIST_TOOL, TOOL_PREFIX
from proxy import RelayServer


def media(mime, data, name=None):
    payload = {"mimeType": mime, "data": base64.b64encode(data).decode()}
    if name:
        payload["displayName"] = name
    return {"inlineData": payload}


def content(role, *parts):
    return {"role": role, "parts": list(parts)}


def user(text, *parts):
    return content("user", {"text": text}, *parts)


def tool_exchange(name, args, result, **response_fields):
    return [
        content("model", {"functionCall": {"name": name, "args": args, "id": "call-1"}, "thoughtSignature": "keep-me"}),
        content("user", {"functionResponse": {"name": name, "id": "call-1", "response": result, **response_fields}}),
    ]


def request(contents, session="test-conversation-0001", with_tools=True):
    declarations = [{"name": "read_file"}]
    if with_tools:
        declarations += [{"name": TOOL_PREFIX + name} for name in (FETCH_TOOL, LIST_TOOL)]
    return {"model": "gemini", "request": {"sessionId": session, "contents": contents, "tools": [{"functionDeclarations": declarations}]}}


@contextmanager
def serving(server):
    with server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server
        finally:
            server.shutdown()
            worker.join()


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.archive = Archive(Path(self.directory.name))
        self.image = media("image/png", b"image-data", "red.png")
        self.video = media("video/mp4", b"video-data", "scene.mp4")

    def test_native_launch_keeps_existing_proxy_and_excludes_loopback(self):
        with patch.dict(os.environ, {"NO_PROXY": "intranet.example,127.0.0.1", "HTTP_PROXY": "inherited"}, clear=True):
            exclude_loopback()
            self.assertEqual(os.environ["HTTP_PROXY"], "inherited")
            self.assertEqual(os.environ["NO_PROXY"], "intranet.example,127.0.0.1,localhost,::1")

    def test_shortcut_startup_uses_its_loopback_address_and_requires_a_marker(self):
        prefix = "/cam-" + "b" * 32
        env = {"CLOUD_CODE_URL": "http://127.0.0.1:47000" + prefix}
        self.assertIsNone(shortcut_listener(env))
        env["AGY_CAM_SIDECAR_RELAY"] = "1"
        self.assertEqual(shortcut_listener(env), (47000, prefix))
        env["CLOUD_CODE_URL"] = "http://192.168.0.1:47000" + prefix
        with self.assertRaises(ValueError):
            shortcut_listener(env)
        direct = run_panel(self.archive, {"action": "sessions"}, routed=False)
        self.assertEqual((direct["online"], direct["routed"]), (True, False))

    def test_session_names_follow_project_renames_and_link_existing_archive(self):
        gemini = Path(self.directory.name) / ".gemini"
        app = gemini / "antigravity"
        conversation_id = "1a111111-0000-0000-0000-111111111111"
        project_id = "2a222222-0000-0000-0000-222222222222"
        transcript = app / "brain" / conversation_id / ".system_generated" / "logs" / "transcript_full.jsonl"
        transcript.parent.mkdir(parents=True)
        project = gemini / "config" / "projects" / f"{project_id}.json"
        project.parent.mkdir(parents=True)
        project.write_text(json.dumps({"name": "动画项目"}), encoding="utf-8-sig")
        prompt = "<USER_REQUEST>\n修改标题\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nThe current local time is: 2026-11-27T11:26:41+08:00\n</ADDITIONAL_METADATA>"
        transcript.write_text(json.dumps({"type": "USER_INPUT", "content": prompt}), encoding="utf-8")
        summary = app / "conversation_summaries.db"
        with closing(sqlite3.connect(summary)) as db, db:
            db.execute("CREATE TABLE conversation_summaries(conversation_id TEXT, title TEXT, project_id TEXT, workspace_uris TEXT, last_user_input_time TEXT)")
            db.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?)", (conversation_id, "修正标题", project_id, "[]", "2026-11-27"))
        scope = fingerprint("older-session-id")
        with self.archive.connect() as db:
            self.archive.observe(db, scope, fingerprint("0\0" + prompt))
            self.archive.set_session(db, scope, "auto", "我的别名")
        with (
            patch("panel.desktop_selection", return_value=("active", conversation_id)),
            patch("panel.desktop_names", side_effect=lambda sessions, **kwargs: desktop_names(sessions, gemini, **kwargs)),
        ):
            entry, = run_panel(self.archive, {"action": "sessions"})["sessions"]
            self.assertEqual((entry["label"], entry["title"], entry["project"]), ("我的别名", "修正标题", "动画项目"))
            self.assertNotIn("conversationId", entry)
            self.assertEqual(entry["desktopKey"], fingerprint(conversation_id))
            self.assertEqual(run_panel(self.archive, {"action": "selection"}),
                             {"status": "active", "key": entry["desktopKey"]})
            transcript.unlink()
            project.write_text(json.dumps({"name": "动画工作室"}), encoding="utf-8")
            with closing(sqlite3.connect(summary)) as db, db:
                db.execute("UPDATE conversation_summaries SET title='改名后的会话'")
            entry, = run_panel(self.archive, {"action": "sessions"})["sessions"]
            self.assertEqual((entry["title"], entry["project"]), ("改名后的会话", "动画工作室"))

    def test_desktop_selection_follows_route_without_using_other_frames_or_windows(self):
        first = "1a111111-0000-0000-0000-111111111111"
        second = "2a222222-0000-0000-0000-222222222222"
        def page(path):
            return {"type": "page", "url": "https://127.0.0.1:49352" + path}

        unrelated = [
            {"type": "iframe", "url": f"https://127.0.0.1:49352/c/{first}"},
            {"type": "page", "url": f"https://another.example/c/{first}"},
            {"type": "page", "url": "data:text/html,splash"},
        ]
        self.assertEqual(selection_from_targets([page(f"/c/{first}?tab=mcp_app"), *unrelated]), ("active", first))
        self.assertEqual(selection_from_targets([page(f"/c/{second}"), *unrelated]), ("active", second))
        self.assertEqual(selection_from_targets([page("/"), *unrelated]), ("none", None))
        self.assertEqual(selection_from_targets([page("/view/mcp-app%2Fantigravity-context-attachment-manager_media%2Fcam_open_panel")]),
                         ("library", None))
        self.assertEqual(selection_from_targets([page(f"/c/{first}"), page(f"/c/{second}")]), ("unavailable", None))
        self.assertEqual(selection_from_targets(unrelated), ("unavailable", None))

    def run_rewrite(self, body):
        report = Report()
        rewrite(body, {}, self.archive, report)
        return report

    def test_first_turn_then_document_edit(self):
        documents = [media(mime, bytes([index]) * 1000) for index, mime in enumerate((
            "audio/wav", "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ))]
        messages = [user("Inspect", self.image, *documents), *tool_exchange(
            "read_file", {"AbsolutePath": "D:\\shots\\scene.mp4"}, {"original": {"$ref": "scene.mp4"}, "text": "keep text"}, parts=[self.video]
        )]
        first = self.run_rewrite(request(messages))
        self.assertEqual((first.seen, first.omitted), (5, 0))
        later = self.run_rewrite(request(messages + [content("model", {"text": "done"}), user("Only edit SH045.md")]))
        self.assertEqual((later.seen, later.omitted, later.restored), (5, 5, 0))
        self.assertIn("red.png", messages[0]["parts"][1]["text"])
        result = messages[2]["parts"][0]["functionResponse"]
        self.assertNotIn("parts", result)
        self.assertIn("scene.mp4", result["response"]["original"])
        self.assertEqual(result["response"]["text"], "keep text")
        self.assertEqual(messages[1]["parts"][0]["thoughtSignature"], "keep-me")

    def test_compaction_restart_and_one_call_restore(self):
        self.run_rewrite(request([user("Inspect", self.image, self.video)]))
        self.archive = Archive(Path(self.directory.name))
        list_name = TOOL_PREFIX + LIST_TOOL
        messages = [user("Find earlier MP4"), *tool_exchange(list_name, {"query": "scene"}, tool_result(LIST_TOOL, {"query": "scene"}))]
        self.assertEqual(self.run_rewrite(request(messages)).listed, 1)
        entry, = messages[-1]["parts"][0]["functionResponse"]["response"]["attachments"]
        self.assertEqual(entry["name"], "scene.mp4")
        fetch_name = TOOL_PREFIX + FETCH_TOOL
        args = {"id": entry["id"]}
        messages = [user("Fetch original"), *tool_exchange(fetch_name, args, tool_result(FETCH_TOOL, args))]
        self.assertEqual(self.run_rewrite(request(messages)).restored, 1)
        self.assertEqual(messages[-1]["parts"][0]["functionResponse"]["parts"], [self.video])
        history = [user("Fetch original"), *tool_exchange(fetch_name, args, tool_result(FETCH_TOOL, args)), content("model", {"text": "done"}), user("Now edit the document")]
        self.assertEqual(self.run_rewrite(request(history)).restored, 0)
        self.assertNotIn("parts", history[2]["parts"][0]["functionResponse"])

    def test_tool_result_before_antigravity_bookkeeping_is_still_fresh(self):
        self.run_rewrite(request([user("Inspect", self.video)]))
        with self.archive.connect() as db:
            scope = hashlib.sha256(b"test-conversation-0001").hexdigest()
            attachment_id = self.archive.list(db, scope, "", 0)["attachments"][0]["id"]
        args = {"id": attachment_id}
        tool = tool_exchange(TOOL_PREFIX + FETCH_TOOL, args, tool_result(FETCH_TOOL, args))
        messages = [user("<USER_REQUEST>\nFetch original\n</USER_REQUEST>"), *tool,
                    content("model", {"text": "Recorded thought", "thought": True}),
                    user("<SYSTEM_MESSAGE>\nInternal bookkeeping\n</SYSTEM_MESSAGE>")]
        self.assertEqual(self.run_rewrite(request(messages)).restored, 1)
        self.assertEqual(tool[1]["parts"][0]["functionResponse"]["parts"], [self.video])

    def test_other_conversation_cannot_fetch_or_list(self):
        self.run_rewrite(request([user("Inspect", self.video)]))
        original = self.archive.list
        with self.archive.connect() as db:
            scope = hashlib.sha256(b"test-conversation-0001").hexdigest()
            attachment_id = original(db, scope, "", 0)["attachments"][0]["id"]
        for tool, args, expected in ((FETCH_TOOL, {"id": attachment_id}, "error"), (LIST_TOOL, {}, "attachments")):
            messages = [user("Try another conversation's attachment"), *tool_exchange(TOOL_PREFIX + tool, args, tool_result(tool, args))]
            result = self.run_rewrite(request(messages, session="another-conversation"))
            response = messages[-1]["parts"][0]["functionResponse"]
            self.assertEqual(result.restored, 0)
            self.assertIn(expected, response["response"])
            if tool == LIST_TOOL:
                self.assertEqual(response["response"]["attachments"], [])

    def test_missing_identity_or_tool_leaves_body_unchanged(self):
        for body in (request([user("Inspect", self.video), user("Next")], session=None), request([user("Inspect", self.video), user("Next")], with_tools=False)):
            before = json.dumps(body)
            self.assertFalse(self.run_rewrite(body).changed)
            self.assertEqual(json.dumps(body), before)

    def test_sibling_tool_media_and_snake_case_uri(self):
        uri = {"file_data": {"mime_type": "audio/wav", "file_uri": "gs://fixture/example.wav"}}
        messages = [user("Inspect"), *tool_exchange("read_file", {"path": "example.wav"}, {}), content("model", {"text": "done"}), user("Only edit")]
        messages[2]["parts"].append(uri)
        result = self.run_rewrite(request(messages))
        self.assertEqual(result.omitted, 1)
        self.assertIn("example.wav", messages[2]["parts"][-1]["text"])

    def test_stdio_tool_schema_and_marker(self):
        greeting = dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(greeting["result"]["protocolVersion"], "2025-06-18")
        tools = dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual({tool["name"] for tool in tools["result"]["tools"]}, {FETCH_TOOL, LIST_TOOL, OPEN_PANEL})
        self.assertTrue(tool_result(FETCH_TOOL, {"id": "../../secret"})["isError"])

    def test_panel_resource_and_settings(self):
        url = "http://127.0.0.1:4000/panel-" + "a" * 32
        with patch.dict(os.environ, {"AGY_CAM_PANEL_URL": url}):
            resource = dispatch({"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": URI}})["result"]["contents"][0]
        self.assertEqual(resource["mimeType"], "text/html;profile=mcp-app")
        self.assertIn(url, resource["text"])
        self.assertEqual(resource["_meta"]["ui"]["csp"]["connectDomains"], ["http://127.0.0.1:4000"])
        report = self.run_rewrite(request([user("Inspect", self.image)]))

        def panel(action, **args):
            return run_panel(self.archive, {"action": action, **args})

        session, = panel("sessions")["sessions"]
        self.assertEqual(session["scope"], report.scope)
        item, = panel("entries", scope=report.scope, kind="image")["attachments"]
        self.assertEqual(item["name"], "red.png")
        self.assertEqual(item["turn"], 1)
        self.assertIsNone(item["turnReceived"])
        self.assertIsNone(item["turnForwarded"])
        self.assertEqual(panel("thumbnail", scope=report.scope, id=item["id"])["data"], self.image["inlineData"]["data"])
        self.assertTrue(panel("session", scope=report.scope, label="Fixtures")["ok"])

    def test_turn_traffic_sums_requests_without_assigning_history_replays_to_old_turns(self):
        first = user("Read an image", self.image)
        archived = self.run_rewrite(request([first]))
        self.archive.record_forward(archived.scope, 5000, 5000, archived)
        tool_followup = self.run_rewrite(request([first, *tool_exchange("read_file", {"path": "scene.mp4"}, {}, parts=[self.video])]))
        self.archive.record_forward(tool_followup.scope, 7000, 6500, tool_followup)
        text = self.run_rewrite(request([first, user("Text only")]))
        self.archive.record_forward(text.scope, 6000, 700, text)
        # An earlier upstream response may finish after the latest turn arrived.
        self.archive.record_forward(archived.scope, 1000, 1000, archived)
        session, = run_panel(self.archive, {"action": "sessions"})["sessions"]
        self.assertEqual(session["currentTurn"], 2)
        self.assertEqual(session["totalReceived"], 19000)
        self.assertEqual(session["totalForwarded"], 13200)
        data = run_panel(self.archive, {"action": "entries", "scope": archived.scope, "kind": "image"})
        item, = data["attachments"]
        self.assertEqual((item["turn"], item["turnReceived"], item["turnForwarded"]), (1, 13000, 12500))

    def test_loopback_panel_accepts_only_its_path_and_origin(self):
        with serving(RelayServer("https://unused.invalid", self.archive)) as relay:
            def call(path, origin):
                with self.subTest(path_matches=path == relay.panel_path, origin=origin):
                    conn = http.client.HTTPConnection("127.0.0.1", relay.server_port, timeout=5)
                    try:
                        conn.request("POST", path, '{"action":"sessions"}', {"Content-Type": "application/json", "Origin": origin})
                        response = conn.getresponse()
                        return response.status, response.read(), response.getheader("Access-Control-Allow-Origin")
                    finally:
                        conn.close()

            status, data, allowed = call(relay.panel_path, "https://example.scf.usercontent.goog")
            self.assertEqual((status, json.loads(data)["sessions"], allowed), (200, [], "https://example.scf.usercontent.goog"))
            self.assertEqual(call(relay.panel_path, "https://unrelated.example")[0], 404)
            self.assertEqual(call("/panel-wrong", "null")[0], 404)

    def test_timeline_merges_retries_and_replays_but_keeps_a_new_read(self):
        first = user("Inspect", self.image)
        read = tool_exchange("read_file", {"path": "scene.mp4"}, {}, parts=[self.video])
        scope = self.run_rewrite(request([deepcopy(first)])).scope
        for _ in range(2):
            self.run_rewrite(request(deepcopy([first, *read])))
        replay = [first, *read, content("model", {"text": "done"})]
        self.run_rewrite(request(deepcopy([*replay, user("Text only")])))
        third = [*replay, user("Text only"), content("model", {"text": "done"}), user("Read again"), *deepcopy(read)]
        self.run_rewrite(request(deepcopy(third)))
        entries = run_panel(self.archive, {"action": "entries", "scope": scope})["attachments"]
        self.assertEqual([(r["turn"], r["name"], r["sources"]) for r in entries], [
            (3, "scene.mp4", READ), (1, "scene.mp4", READ), (1, "red.png", UPLOADED),
        ])
        self.assertEqual(entries[0]["id"], entries[1]["id"])

    def test_timeline_retains_desktop_turn_by_timestamp_after_compaction(self):
        def desktop_turn(text, seconds, *parts):
            return user(f"<USER_REQUEST>\n{text}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\n"
                        f"The current local time is: 2026-11-27T11:26:{seconds}+08:00.\n</ADDITIONAL_METADATA>", *parts)

        first = desktop_turn("Inspect", "10", self.image)
        second = desktop_turn("Inspect", "11", self.video)
        report = self.run_rewrite(request(deepcopy([first, second])))
        with self.archive.connect() as db:
            attachment_id = next(item["id"] for item in self.archive.list(db, report.scope, "red.png", 0)["attachments"])
        args = {"id": attachment_id}
        fetched = tool_exchange(TOOL_PREFIX + FETCH_TOOL, args, tool_result(FETCH_TOOL, args))
        # Nothing should be grouped under the pre-turn summary.
        archived_context = user("Compacted summary", media("application/pdf", b"old PDF", "unknown.pdf"))
        third = desktop_turn("Fetch again", "12")
        for _ in range(2):
            self.run_rewrite(request(deepcopy([archived_context, second, third, *fetched])))
        entries = run_panel(self.archive, {"action": "entries", "scope": report.scope})["attachments"]
        self.assertEqual([(r["turn"], r["name"], r["sources"]) for r in entries], [
            (3, "red.png", FETCHED), (2, "scene.mp4", UPLOADED), (1, "red.png", UPLOADED), (None, "unknown.pdf", 0),
        ])

    def test_legacy_archive_is_visible_and_old_selections_are_inactive(self):
        scope = hashlib.sha256(b"test-conversation-0001").hexdigest()
        with self.archive.connect() as db:
            self.archive.observe(db, scope, None)
            self.archive.set_session(db, scope, "manual", "Retained label")
            attachment_id, _ = self.archive.save(db, scope, "scene.mp4", "video/mp4", self.video)
            db.execute("INSERT INTO attachment_options(scope,id,pinned,queued) VALUES (?,?,1,'queued')", (scope, attachment_id))
        self.archive = Archive(Path(self.directory.name))
        body = request([user("Only text")])
        report = self.run_rewrite(body)
        self.assertFalse(report.changed)
        session, = run_panel(self.archive, {"action": "sessions"})["sessions"]
        self.assertEqual((session["mode"], session["label"]), ("auto", "Retained label"))
        item, = run_panel(self.archive, {"action": "entries", "scope": scope})["attachments"]
        self.assertIsNone(item["turn"])
        args = {"id": attachment_id}
        fetch = tool_exchange(TOOL_PREFIX + FETCH_TOOL, args, tool_result(FETCH_TOOL, args))
        self.assertEqual(self.run_rewrite(request([user("Fetch"), *fetch])).restored, 1)

    def test_legacy_timeline_backfills_only_exact_unambiguous_local_matches(self):
        conversation = "99c09f01-0eda-41b8-8d92-f8ae8b4d6474"
        scope = fingerprint(conversation)
        gemini = Path(self.directory.name) / ".gemini"
        folder = gemini / "antigravity" / "brain" / conversation / ".system_generated" / "logs"
        (folder / "media").mkdir(parents=True)
        (folder / "media" / "step_2_media_0.png").write_bytes(b"image-data")
        (folder / "media" / "step_2_media_1.mp4").write_bytes(b"different-video")
        (folder / "media" / "step_4_media_0.mp4").write_bytes(b"video-data")
        user_input = {"type": "USER_INPUT", "content": "<USER_REQUEST>\nRead image\n</USER_REQUEST>\n"
                      "<ADDITIONAL_METADATA>\nThe current local time is: 2026-11-27T11:26:10+08:00.\n</ADDITIONAL_METADATA>"}
        read = {"type": "GENERIC", "status": "DONE", "step_index": 2, "content": "Showing binary file of type",
                "media": [{"uri": "file:///local/step_2_media_0.png", "mime_type": "image/png"},
                          {"uri": "file:///local/step_2_media_1.mp4", "mime_type": "video/mp4"},
                          {"uri": "file:///local/step_4_media_0.mp4", "mime_type": "video/mp4"}]}
        read["content"] += " image/png"
        (folder / "transcript_full.jsonl").write_text("\n".join(map(json.dumps, [user_input, read])), encoding="utf-8")
        with self.archive.connect() as db:
            self.archive.observe(db, scope, None)
            self.archive.save(db, scope, "renamed.png", "image/png", self.image)
            self.archive.save(db, scope, "scene.mp4", "video/mp4", self.video)
        for _ in range(2):
            with self.archive.connect() as db:
                backfill(self.archive, db, scope, conversation, gemini=gemini)
                entries = self.archive.entries(db, scope, "", "", 0)["attachments"]
                self.assertEqual([(r["turn"], r["name"], r["sources"]) for r in entries], [
                    (1, "renamed.png", READ), (None, "scene.mp4", 0),
                ])

    def test_paused_mode_passthrough(self):
        initial = self.run_rewrite(request([user("Inspect", self.video)]))
        with self.archive.connect() as db:
            self.archive.set_session(db, initial.scope, "paused", "Test")
        report = self.run_rewrite(request([user("Inspect", self.video), user("Next")]))
        self.assertEqual((report.state, report.omitted), ("paused", 0))

    def test_windows_open_uses_only_the_archived_bytes_and_a_safe_extension(self):
        supported = [media("image/png", b"image", r"..\..\CON.cmd"), self.video,
                     media("audio/wav", b"audio", "clip.wav"), media("application/pdf", b"document", "report.pdf")]
        unsafe = media("image/svg+xml", b"<svg/>", "danger.hta")
        remote = {"fileData": {"mimeType": "video/mp4", "fileUri": "https://example.invalid/original"}}
        report = self.run_rewrite(request([user("Inspect", *supported, unsafe, remote)]))
        with self.archive.connect() as db:
            entries = {name: attachment_id for attachment_id, name in db.execute("SELECT id,name FROM attachments")}
        with patch("panel.os.startfile", create=True) as startfile:
            for asset, suffix in zip(supported, (".png", ".mp4", ".wav", ".pdf")):
                with self.subTest(suffix=suffix):
                    data = asset["inlineData"]
                    result = run_panel(self.archive, {"action": "open", "scope": report.scope, "id": entries[data["displayName"].split("\\")[-1]], "path": "arbitrary.exe"})
                    self.assertEqual(result, {"ok": True})
                    opened = Path(startfile.call_args.args[0])
                    self.assertEqual(opened.parent, self.archive.path.parent / "media")
                    self.assertEqual(opened.suffix, suffix)
                    self.assertEqual(opened.read_bytes(), base64.b64decode(data["data"]))
            for scope, attachment_id in ((report.scope, entries["danger.hta"]), (report.scope, entries[""]),
                                         (report.scope, "../elsewhere"), ("f" * 64, entries["report.pdf"])):
                self.assertIn("error", run_panel(self.archive, {"action": "open", "scope": scope, "id": attachment_id}))
            self.assertEqual(startfile.call_count, 4)

        self.assertFalse((self.archive.path.parent / "preview-cache").exists())

    def test_legacy_archive_migrates_on_open_and_preserves_original(self):
        directory = Path(self.directory.name) / "old"
        directory.mkdir()
        scope = "a" * 64
        original = {"inline_data": self.video["inlineData"], "video_metadata": {"fps": 1}}
        payload = json.dumps(original, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
        attachment_id = "CAM-" + hashlib.sha256(payload).hexdigest()[:32]
        with closing(sqlite3.connect(directory / "attachments.sqlite3")) as db, db:
            db.execute("""CREATE TABLE attachments (scope TEXT, id TEXT, name TEXT, mime TEXT, payload BLOB,
                          PRIMARY KEY (scope, id))""")
            db.execute("INSERT INTO attachments VALUES (?, ?, ?, ?, ?)",
                       (scope, attachment_id, "scene.mp4", "video/mp4", payload))
        archive = Archive(directory)
        with archive.connect() as db:
            self.assertEqual(archive.load(db, scope, attachment_id), ("scene.mp4", "video/mp4", original))
        action = {"action": "open", "scope": scope, "id": attachment_id}
        with patch("panel.os.startfile", create=True) as startfile:
            self.assertEqual(run_panel(archive, action), {"ok": True})
            first_path = Path(startfile.call_args.args[0])
            self.assertEqual(run_panel(archive, action), {"ok": True})
            self.assertEqual(Path(startfile.call_args.args[0]), first_path)
        self.assertEqual(first_path.read_bytes(), b"video-data")
        self.assertFalse((directory / "preview-cache").exists())
        with archive.connect() as db:
            stored, size = db.execute("SELECT payload, file_size FROM attachments").fetchone()
            self.assertNotIn(base64.b64encode(b"video-data"), stored)
            self.assertEqual(size, len(b"video-data"))
            self.assertEqual(archive.load(db, scope, attachment_id), ("scene.mp4", "video/mp4", original))
            other = "b" * 64
            archive.save(db, other, "renamed.mp4", "video/mp4", media("video/mp4", b"video-data", "renamed.mp4"))
            self.assertEqual(list((directory / "media").iterdir()), [first_path])

    def test_chunked_gzip_request_and_streaming_forward(self):
        seen = {}

        class FakeGoogle(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                seen["path"] = self.path
                seen["body"] = self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b"data: forwarded\n\n")

        body = request([user("Inspect", media("video/mp4", b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(3000)))), user("Only edit")])
        compressed = gzip.compress(json.dumps(body).encode())
        with serving(ThreadingHTTPServer(("127.0.0.1", 0), FakeGoogle)) as target:
            with serving(RelayServer(f"http://127.0.0.1:{target.server_port}", self.archive)) as relay:
                conn = http.client.HTTPConnection("127.0.0.1", relay.server_port, timeout=5)
                conn.request("POST", relay.prefix + "/v1internal:streamGenerateContent?alt=sse", iter((compressed[:500], compressed[500:])), {"Content-Encoding": "gzip"}, encode_chunked=True)
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b"data: forwarded\n\n")
                conn.close()
                unauthorized = http.client.HTTPConnection("127.0.0.1", relay.server_port, timeout=5)
                unauthorized.request("POST", "/v1internal:streamGenerateContent", b"{}")
                self.assertEqual(unauthorized.getresponse().status, 404)
                unauthorized.close()
        self.assertEqual(seen["path"], "/v1internal:streamGenerateContent?alt=sse")
        self.assertLess(len(seen["body"]), len(compressed) // 20)
        saved = json.loads(gzip.decompress(seen["body"]))
        self.assertIn("[Archived attachment CAM-", saved["request"]["contents"][0]["parts"][1]["text"])


if __name__ == "__main__":
    unittest.main()
