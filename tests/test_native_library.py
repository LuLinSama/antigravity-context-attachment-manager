import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

import mcp_server
import panel
from archive import Archive
from conversation_names import desktop_names, fingerprint
from desktop_selection import selection_from_targets
from transcript_index import delegated_turn
from guard_read import guard
from record_read import record_event
from usage import Metrics, Usage
from track_usage import lifecycle


FIRST = "1a111111-0000-0000-0000-111111111111"
SECOND = "2a222222-0000-0000-0000-222222222222"


class NativeLibraryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.data = self.root / "archive"
        self.archive = Archive(self.data)

    def transcript(self, conversation=FIRST):
        path = self.root / "brain" / conversation / ".system_generated" / "logs" / "transcript.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps({"type": "USER_INPUT", "step_index": index})
                                  for index in (0, 4)), encoding="utf-8")
        return path

    def entries(self, conversation=FIRST, **args):
        with self.archive.connect() as db:
            return self.archive.entries(db, fingerprint(conversation), "", args.get("kind", ""), 0)["attachments"]

    def delegate(self, agent="cam-media", launches=True):
        db = sqlite3.connect(self.root / "conversation_summaries.db")
        with db:
            db.execute("CREATE TABLE conversation_summaries (conversation_id, agent_name, parent_conversation_id)")
            db.executemany("INSERT INTO conversation_summaries VALUES (?, ?, ?)", [(FIRST, "", None), (SECOND, agent, FIRST)])
        db.close()
        rows = [
            {"step_index": 0, "type": "USER_INPUT"},
            {"step_index": 3, "type": "USER_INPUT"},
            {"step_index": 4, "type": "PLANNER_RESPONSE", "source": "MODEL", "status": "DONE",
             "tool_calls": [{"name": "invoke_subagent", "args": {"TypeName": "cam-media"}}]},
            {"step_index": 5, "type": "GENERIC", "source": "MODEL", "status": "DONE",
             "content": "Created the following subagent:\n" + json.dumps({"conversationId": SECOND})},
            {"step_index": 6, "type": "USER_INPUT"},
        ]
        log = self.transcript()
        log.write_text("\n".join(map(json.dumps, rows if launches else rows[:3])), encoding="utf-8")
        return log, rows

    def event(self, path, step=2, conversation=FIRST, **fields):
        return {"toolCall": {"name": "read_file", "args": {"Path": str(path)}},
                "conversationId": conversation, "stepIdx": step, **fields}

    def test_main_text_allowed_and_media_denied_before_read(self):
        self.delegate()
        for name in ("code.py", "notes.md", "table.csv", "data.json"):
            self.assertEqual(guard(self.event(self.root / name), self.data, self.root)["decision"], "allow")
        for name in ("MOVIE.MP4", "sample.pdf", "sample.jpg", "result.wav", "notes.docx"):
            denied = guard(self.event(self.root / name), self.data, self.root)
            self.assertEqual(denied["decision"], "deny")
            self.assertIn("invoke_subagent", denied["reason"])
        self.assertEqual(guard({"toolCall": {"name": "invoke_subagent"}, "conversationId": FIRST}, self.data, self.root)["decision"], "allow")

    def test_child_uses_one_read_never_delegates_or_reuses(self):
        self.delegate()
        doc = self.root / "sample.pdf"
        event = self.event(doc, conversation=SECOND)
        self.assertEqual(guard(event, self.data, self.root)["decision"], "allow")
        self.assertEqual(guard(event, self.data, self.root)["decision"], "allow")  # duplicate installed hooks
        for denied in (self.event(doc, step=3, conversation=SECOND),
                       self.event(doc.with_suffix(".wav"), conversation=SECOND),
                       {**event, "toolCall": {"name": "invoke_subagent"}},
                       self.event(doc, conversation="3a333333-0000-0000-0000-333333333333")):
            reply = guard(denied, self.data, self.root)
            self.assertEqual(reply["decision"], "deny")
            self.assertNotIn("invoke_subagent", reply["reason"])

    def test_finished_child_cannot_be_reawakened_even_with_traffic_disabled(self):
        self.delegate()
        self.data.mkdir(exist_ok=True)
        (self.data / "traffic.json").write_text('{"enabled": false}', encoding="utf-8")
        event = {"conversationId": SECOND, "invocationNum": 0, "initialNumSteps": 1}
        self.assertIsNone(lifecycle(event, "pre", self.data, self.root))
        self.assertEqual(lifecycle({**event, "fullyIdle": True}, "stop", self.data, self.root), {"decision": "stop"})
        restart = lifecycle(event, "pre", self.data, self.root)
        self.assertEqual(restart["terminationBehavior"], "terminate")
        self.assertIn("errorMessage", restart["injectSteps"][0])
        self.assertEqual(guard(self.event(self.root / "sample.pdf", conversation=SECOND), self.data, self.root)["decision"], "deny")

    def test_confirmed_native_kill_releases_missing_child_stop_window(self):
        log, rows = self.delegate()
        usage, config = Usage(self.data), Metrics()
        key = (SECOND, 0, 1)
        owner = (FIRST, 2, fingerprint("turn"), "child")
        with patch("usage.sample", return_value=(100, 500)):
            usage.begin(key, owner, config)
        command = {"step_index": 7, "type": "PLANNER_RESPONSE", "source": "MODEL", "status": "DONE",
                   "tool_calls": [{"name": "manage_subagents", "args": {"Action": "kill", "ConversationIds": [SECOND]}}]}
        result = {"step_index": 8, "type": "GENERIC", "source": "MODEL", "status": "DONE",
                  "content": "Successfully killed 1 subagent(s) and their descendants."}
        log.write_text("\n".join(map(json.dumps, [*rows, command, {**result, "status": "ERROR"}])), encoding="utf-8")
        usage.reap(self.root, FIRST)
        self.assertFalse(usage.closed(SECOND))
        log.write_text("\n".join(map(json.dumps, [*rows, command, result])), encoding="utf-8")
        usage.reap(self.root, FIRST)
        self.assertTrue(usage.closed(SECOND))
        self.assertEqual(usage.turns(FIRST, [])[0]["child"]["status"], "incomplete")
        main = (FIRST, 0, 2)
        with patch("usage.sample", side_effect=[(110, 510), (130, 520)]):
            usage.begin(main, (FIRST, 3, fingerprint("next"), "main"), config)
            usage.end(main, None, config)
        self.assertEqual(usage.turns(FIRST, [])[0]["main"]["bytes"], 20)

    def test_successful_child_read_reconciles_exact_launch_without_copying(self):
        log, rows = self.delegate(launches=False)
        video = self.root / "素材.mp4"
        with video.open("wb") as stream:
            stream.truncate(50 * 1024 * 1024)
        event = self.event(video, conversation=SECOND)
        self.assertEqual(guard(event, self.data, self.root)["decision"], "allow")
        record_event(event, self.data, self.root)
        self.assertEqual(self.entries(), [])
        log.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
        record_event(event, self.data, self.root)
        item, = self.entries(kind="video")
        self.assertEqual((item["name"], item["turn"]), (video.name, 2))
        self.assertEqual(self.entries(SECOND), [])
        self.assertFalse((self.data / "media").exists())
        with self.archive.connect() as db:
            self.assertEqual(db.execute("SELECT path FROM local_files").fetchone(), (str(video),))
            self.assertEqual(db.execute("SELECT max(length(payload)) FROM attachments").fetchone(), (2,))
        args = {"action": "open", "scope": fingerprint(FIRST), "id": item["id"]}
        with patch("panel.os.startfile", create=True) as start:
            self.assertEqual(panel.run(self.archive, args), {"ok": True})
            start.assert_called_once_with(str(video))
            video.unlink()
            self.assertIn("error", panel.run(self.archive, args))

    def test_failed_unreserved_main_or_direct_child_prompt_is_not_indexed(self):
        self.delegate()
        file = self.root / "example.pdf"
        file.write_bytes(b"%PDF synthetic")
        event = self.event(file, conversation=SECOND)
        record_event(event, self.data, self.root)
        guard(event, self.data, self.root)
        record_event({**event, "error": "failed"}, self.data, self.root)
        record_event(self.event(file), self.data, self.root)
        self.transcript(SECOND)
        record_event(event, self.data, self.root)
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.entries(SECOND), [])

    def test_launch_parser_rejects_unverified_results(self):
        log, rows = self.delegate()
        self.assertEqual(delegated_turn(self.root, FIRST, SECOND, directory=self.data)[0], 2)
        for key, value in (("status", "ERROR"), ("source", "USER_EXPLICIT")):
            altered = [*rows[:3], {**rows[3], key: value}, rows[4]]
            log.write_text("\n".join(map(json.dumps, altered)), encoding="utf-8")
            self.assertIsNone(delegated_turn(self.root, FIRST, SECOND, directory=self.data))

    def test_hook_commands_use_documented_stdio(self):
        env = {**os.environ, "LOCALAPPDATA": str(self.root), "XDG_DATA_HOME": str(self.root)}
        for script, args, event, expected in (
            ("guard_read.py", [], self.event(self.root / "notes.txt"), {"decision": "allow"}),
            ("record_read.py", [], self.event(self.root / "note.pdf"), {}),
            ("track_usage.py", ["stop"], {"conversationId": FIRST, "fullyIdle": True}, {"decision": "stop"}),
        ):
            reply = subprocess.run([sys.executable, str(ROOT / "scripts" / script), *args],
                                   input=json.dumps(event), text=True, env=env, capture_output=True, check=True)
            self.assertEqual(json.loads(reply.stdout), expected)
            self.assertEqual(reply.stderr, "")

    def test_legacy_rows_remain_but_are_not_shown_or_copied(self):
        scope = fingerprint(FIRST)
        old_id = "CAM-" + "f" * 32
        with self.archive.connect() as db:
            self.archive.touch(db, FIRST, 1)
            db.execute("INSERT INTO attachments VALUES (?, ?, ?, ?, ?)",
                       (scope, old_id, "old.png", "image/png", b"inline"))
            db.execute("INSERT INTO local_files VALUES (?, ?, ?)", (scope, old_id, str(self.root / "old.png")))
            db.execute("INSERT INTO turns VALUES (?, ?, ?)", (scope, "old-turn", 1))
            db.execute("INSERT INTO turn_attachments VALUES (?, ?, ?, ?)", (scope, "old-turn", old_id, 2))
        self.assertEqual(self.entries(), [])
        self.assertIn("error", panel.run(self.archive, {"action": "open", "scope": scope, "id": old_id}))
        self.assertEqual(panel.run(self.archive, {"action": "sessions"})["sessions"], [])
        with self.archive.connect() as db:
            self.assertEqual(db.execute("SELECT payload FROM attachments").fetchone(), (b"inline",))
        self.assertFalse((self.data / "media").exists())

    def test_traffic_uses_the_native_scope_not_a_legacy_mapping(self):
        legacy = fingerprint("older-session-id")
        scope = fingerprint(FIRST)
        with self.archive.connect() as db:
            db.execute("INSERT INTO sessions(scope,label) VALUES (?,?)", (legacy, "旧会话"))
            db.execute("INSERT INTO session_names(scope, conversation_id) VALUES (?, ?)", (legacy, FIRST))
        usage, config = Usage(self.data), Metrics()
        owner = FIRST, 1, fingerprint("turn"), "main"
        with patch("usage.sample", side_effect=[(110, 100), (130, 120)]):
            usage.begin((FIRST, 0, 1), owner, config)
            usage.end((FIRST, 0, 1), owner, config)
        with patch("panel.desktop_selection", return_value=("active", FIRST)), patch("panel.desktop_names", return_value={}):
            session, = panel.run(self.archive, {"action": "sessions"})["sessions"]
            self.assertEqual((session["scope"], session["label"]), (scope, ""))
            self.assertNotIn("conversationId", session)
            self.assertEqual(session["count"], 0)
        turn, = panel.run(self.archive, {"action": "traffic", "scope": scope})["turns"]
        self.assertEqual(turn["main"]["bytes"], 20)
        self.assertEqual(turn["child"]["status"], "unobserved")
        self.assertIn("error", panel.run(self.archive, {"action": "traffic", "scope": legacy}))

    def test_delete_clears_only_cam_archive_and_blocks_stale_observations(self):
        with patch("panel.desktop_selection", return_value=("none", None)), patch("panel.desktop_names", return_value={}):
            original = self.root / "original.png"
            original.write_bytes(b"media")
            native_log = self.transcript()
            transcript = native_log.read_bytes()
            scope, other_scope = fingerprint(FIRST), fingerprint(SECOND)
            old_turn = (1, fingerprint("old turn"))
            new_turn = (2, fingerprint("new turn"))
            with self.archive.connect() as db:
                for owner in (FIRST, SECOND):
                    self.archive.reference(db, owner, original, "image/png", old_turn, 10)
                db.execute("UPDATE sessions SET label='旧别名' WHERE scope=?", (scope,))
            self.archive.queue_read(SECOND, FIRST, original, "image/png", 11)
            usage, config = Usage(self.data), Metrics()
            main = (FIRST, *old_turn, "main")
            child = (FIRST, *old_turn, "child")
            other = (SECOND, *old_turn, "main")
            with patch("usage.sample", return_value=(100, None)), patch("usage.time.time", return_value=20):
                usage.begin((FIRST, 0, 1), main, config)
                usage.end((FIRST, 0, 1), main, config)
                usage.begin((SECOND, 0, 1), other, config)
                usage.end((SECOND, 0, 1), other, config)
                usage.begin((FIRST, 1, 2), main, config)
                usage.begin((SECOND, 1, 2), None, config)

            args = {"action": "delete_session", "scope": scope}
            envelope = {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
            with patch("mcp_server.archive", return_value=self.archive):
                rpc = {**envelope, "params": {"name": panel.OPEN_PANEL, "arguments": args}}
                self.assertTrue(mcp_server.dispatch(rpc)["result"]["isError"])
                self.assertTrue(self.entries())
                with patch("panel.time.time", return_value=30):
                    args["capability"] = panel.CAPABILITY
                    self.assertEqual(mcp_server.dispatch(rpc)["result"]["structuredContent"], {"ok": True})
            with self.archive.connect() as db:
                for table in ("sessions", "session_names", "attachments", "local_files", "turn_attachments", "turns"):
                    self.assertIsNone(db.execute(f"SELECT 1 FROM {table} WHERE scope=?", (scope,)).fetchone(), table)
                self.assertIsNone(db.execute("SELECT 1 FROM pending_reads WHERE parent=?", (FIRST,)).fetchone())
                self.assertFalse(self.archive.reference(db, FIRST, original, "image/png", old_turn, 25))
            self.archive.queue_read(SECOND, FIRST, original, "image/png", 25)
            usage.end((FIRST, 1, 2), main, config)
            usage.end((SECOND, 1, 2), child, config)
            self.assertFalse(usage.turns(FIRST, []))
            self.assertTrue(usage.turns(SECOND, []))
            self.assertEqual([item["scope"] for item in panel.run(self.archive, {"action": "sessions"})["sessions"]], [other_scope])
            self.assertEqual(original.read_bytes(), b"media")
            self.assertEqual(native_log.read_bytes(), transcript)

            with self.archive.connect() as db:
                self.assertTrue(self.archive.reference(db, FIRST, original, "image/png", new_turn, 40))
                self.assertIsNone(db.execute("SELECT 1 FROM pending_reads WHERE parent=?", (FIRST,)).fetchone())
            with patch("usage.sample", return_value=(100, None)), patch("usage.time.time", return_value=40):
                new_owner = (FIRST, *new_turn, "main")
                usage.begin((FIRST, 2, 3), new_owner, config)
                usage.end((FIRST, 2, 3), new_owner, config)
            turns = usage.turns(FIRST, [1])
            self.assertEqual([turn["turn"] for turn in turns], [2])
            self.assertEqual(self.entries()[0]["turn"], 2)
            self.assertEqual(len(self.entries(SECOND)), 1)

    def test_titles_resolve_by_known_native_conversation_ids(self):
        gemini = self.root / ".gemini"
        catalogue = gemini / "antigravity" / "conversation_summaries.db"
        catalogue.parent.mkdir(parents=True)
        db = sqlite3.connect(catalogue)
        with db:
            db.execute("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title, project_id, workspace_uris)")
            db.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?, ?)",
                       (FIRST, "Native title", None, '["file:///workspace/example"]'))
        db.close()
        scope = fingerprint(FIRST)
        sessions = [{"scope": scope, "conversationId": FIRST},
                    {"scope": fingerprint(SECOND), "conversationId": SECOND}]
        self.assertEqual(desktop_names(sessions, gemini), {scope: {"title": "Native title", "project": "example"}})

    def test_stdio_exposes_only_panel_tools_and_requires_view_capability(self):
        def rpc(method, **params):
            return mcp_server.dispatch({"id": 1, "jsonrpc": "2.0", "method": method, "params": params})["result"]
        tools = rpc("tools/list")["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {panel.OPEN_PANEL})
        self.assertTrue(all(tool["_meta"]["ui"]["visibility"] == ["app"] for tool in tools))
        html = rpc("resources/read", uri=panel.URI)["contents"][0]["text"]
        self.assertIn(panel.CAPABILITY, html)
        self.assertNotIn("fetch(", html)
        self.assertNotIn("connectDomains", html)
        with patch("mcp_server.archive", return_value=self.archive) as load:
            opened = rpc("tools/call", name=panel.OPEN_PANEL)
            self.assertFalse(opened["isError"])
            load.assert_not_called()
            action = {"action": "sessions"}
            unauthorized = rpc("tools/call", name=panel.OPEN_PANEL, arguments=action)
            self.assertTrue(unauthorized["isError"])
            load.assert_not_called()
            action["capability"] = panel.CAPABILITY
            authorized = rpc("tools/call", name=panel.OPEN_PANEL, arguments=action)
            self.assertEqual(authorized["structuredContent"], {"online": True, "sessions": []})

    def test_selection_rejects_ambiguous_windows(self):
        def page(path):
            return {"type": "page", "url": "https://127.0.0.1:49352" + path}
        self.assertEqual(selection_from_targets([page(f"/c/{FIRST}")]), ("active", FIRST))
        self.assertEqual(selection_from_targets([page(f"/c/{SECOND}")]), ("active", SECOND))
        self.assertEqual(selection_from_targets([page(f"/c/{FIRST}"), page(f"/c/{SECOND}")]), ("unavailable", None))

    def test_native_selection_keys_match_existing_media_sessions(self):
        with self.archive.connect() as db:
            for cid in (FIRST, SECOND):
                self.assertTrue(self.archive.reference(db, cid, self.root / "file.pdf", "application/pdf",
                                                       (1, fingerprint("turn")), 1))
        library = Archive(self.data)
        with patch("panel.desktop_names", return_value={}):
            sessions = panel.run(library, {"action": "sessions"})["sessions"]
        for cid in (FIRST, SECOND):
            with patch("panel.desktop_selection", return_value=("active", cid)):
                selection = panel.run(library, {"action": "selection"})
            session, = (session for session in sessions if session["scope"] == selection["key"])
            self.assertEqual(session["count"], 1)
            self.assertNotIn("conversationId", session)


if __name__ == "__main__":
    unittest.main()
