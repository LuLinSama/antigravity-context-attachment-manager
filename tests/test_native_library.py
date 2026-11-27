import base64
import json
import os
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
from conversation_names import fingerprint
from desktop_selection import selection_from_targets
from record_read import record_event


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

    def event(self, path, step=2, conversation=FIRST, **fields):
        return {"toolCall": {"name": "read_file", "args": {"Path": str(path)}},
                "conversationId": conversation, "stepIdx": step,
                "transcriptPath": str(self.transcript(conversation)), **fields}

    def entries(self, conversation=FIRST, **args):
        return panel.run(self.archive, {"action": "entries", "scope": fingerprint(conversation), **args})["attachments"]

    def test_large_file_reference_is_deduplicated_per_turn_without_copying(self):
        video = self.root / "素材.mp4"
        with video.open("wb") as stream:
            stream.truncate(50 * 1024 * 1024)
        for step in (2, 2, 6):
            record_event(self.event(video, step), self.data)
        record_event(self.event(video, conversation=SECOND), self.data)
        entries = self.entries(kind="video")
        self.assertEqual([(item["turn"], item["sources"]) for item in entries], [(2, 2), (1, 2)])
        self.assertEqual(entries[0]["id"], entries[1]["id"])
        self.assertEqual(len(self.entries(SECOND)), 1)
        self.assertFalse((self.data / "media").exists())
        with self.archive.connect() as db:
            self.assertEqual(db.execute("SELECT max(length(payload)) FROM attachments").fetchone(), (2,))
        args = {"action": "open", "scope": fingerprint(FIRST), "id": entries[0]["id"]}
        with patch("panel.os.startfile", create=True) as start:
            self.assertEqual(panel.run(self.archive, args), {"ok": True})
            start.assert_called_once_with(str(video))
            video.unlink()
            self.assertIn("error", panel.run(self.archive, args))

    def test_failed_reads_untrusted_paths_and_non_file_tools_are_ignored(self):
        pdf = self.root / "document.pdf"
        pdf.write_bytes(b"test")
        record_event(self.event(pdf, error="failed"), self.data)
        record_event(self.event(pdf, toolCall={"name": "run_command", "args": {"Path": str(pdf)}}), self.data)
        record_event(self.event(Path("relative.pdf")), self.data)
        shortcut = pdf.with_suffix(".lnk")
        shortcut.write_bytes(b"test")
        record_event(self.event(shortcut), self.data)
        with self.archive.connect() as db:
            self.assertEqual(self.archive.sessions(db), [])

    def test_hook_uses_stdio_and_supports_documented_event_without_transcript(self):
        file = self.root / "note.pdf"
        file.write_bytes(b"test")
        event = self.event(file, transcriptPath=None, toolCall={"name": "view_file", "args": {"AbsolutePath": str(file)}})
        env = {**os.environ, "LOCALAPPDATA": str(self.root), "XDG_DATA_HOME": str(self.root)}
        reply = subprocess.run([sys.executable, str(ROOT / "scripts" / "record_read.py")],
                               input=json.dumps(event), text=True, env=env, capture_output=True, check=True)
        self.assertEqual(json.loads(reply.stdout), {})
        self.assertEqual(reply.stderr, "")
        recorded = Archive(self.root / "antigravity-context-attachment-manager")
        with recorded.connect() as db:
            item, = recorded.entries(db, fingerprint(FIRST), "note", "document", 0)["attachments"]
            self.assertEqual((item["name"], item["turn"], item["sources"]), (file.name, None, 2))

    def test_hook_maps_full_transcript_and_uses_default_logs_when_omitted(self):
        first = self.root / "first.md"
        second = self.root / "second.md"
        for file in (first, second):
            file.write_text("synthetic", encoding="utf-8")
        path = self.transcript()
        logs = self.root / ".gemini" / "antigravity" / path.relative_to(self.root).parent
        logs.mkdir(parents=True)
        full = logs / "transcript_full.jsonl"
        path.replace(full)
        record_event(self.event(first, step=2, transcriptPath=str(full)), self.data)
        with patch("record_read.Path.home", return_value=self.root):
            record_event(self.event(second, step=6, transcriptPath=None), self.data)
        self.assertEqual([(item["name"], item["turn"]) for item in self.entries()],
                         [(second.name, 2), (first.name, 1)])

    def test_legacy_inline_media_preview_and_lazy_migration(self):
        scope, attachment_id = fingerprint(FIRST), "CAM-" + "a" * 32
        original = b"legacy-image"
        data = base64.b64encode(original).decode("ascii")
        payload = json.dumps({"inlineData": {"mimeType": "image/png", "data": data}})
        with self.archive.connect() as db:
            db.execute("INSERT INTO sessions(scope) VALUES (?)", (scope,))
            db.execute("INSERT INTO attachments(scope,id,name,mime,payload) VALUES (?,?,?,?,?)",
                       (scope, attachment_id, "legacy.png", "image/png", payload))
        args = {"scope": scope, "id": attachment_id}
        self.assertEqual(panel.run(self.archive, {"action": "thumbnail", **args})["data"], data)
        with patch("panel.os.startfile", create=True) as start:
            for _ in range(2):
                self.assertEqual(panel.run(self.archive, {"action": "open", **args}), {"ok": True})
            opened = [Path(call.args[0]) for call in start.call_args_list]
            self.assertEqual(opened[0], opened[1])
            self.assertEqual(opened[0].suffix, ".png")
            self.assertEqual(opened[0].read_bytes(), original)
            self.assertEqual(list((self.data / "media").iterdir()), [opened[0]])
            opened[0].chmod(0o600)

    def test_linked_old_scope_stays_single_and_can_be_selected(self):
        legacy = fingerprint("older-session-id")
        with self.archive.connect() as db:
            db.execute("INSERT INTO sessions(scope,label) VALUES (?,?)", (legacy, "旧会话"))
            self.archive.link_conversations(db, {legacy: FIRST})
        document = self.root / "sample.pdf"
        document.write_bytes(b"doc")
        record_event(self.event(document), self.data)
        with patch("panel.desktop_selection", return_value=("active", FIRST)), patch("panel.desktop_names", return_value=({}, {})):
            session, = panel.run(self.archive, {"action": "sessions"})["sessions"]
            self.assertEqual((session["scope"], session["label"], session["desktopKey"]), (legacy, "旧会话", fingerprint(FIRST)))
            self.assertNotIn("conversationId", session)
            self.assertEqual(panel.run(self.archive, {"action": "selection"}),
                             {"status": "active", "key": session["desktopKey"]})

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


if __name__ == "__main__":
    unittest.main()
