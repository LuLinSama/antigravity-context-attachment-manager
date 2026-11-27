import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import test_native_library as fixtures
from test_native_library import FIRST, SECOND
from conversation_names import fingerprint
from guard_read import guard
from media_delegation import transcript_paths
from panel import run
from track_usage import lifecycle
from transcript_index import INDEX_PENDING, TranscriptIndex, delegated_turn, user_turn
from usage import Metrics, Usage


def row(kind, index, **values):
    return {"type": kind, "step_index": index, **values}


def line(value):
    return json.dumps(value).encode() + b"\n"


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name).resolve()
        self.data = self.root / "cache"
        self.full, self.compact = transcript_paths(self.root, FIRST)
        self.full.parent.mkdir(parents=True)

    def turn(self, step=None):
        return user_turn(self.root, FIRST, step, directory=self.data)

    def test_large_full_log_then_append_reads_only_new_records(self):
        prompt = "<USER_REQUEST>private sample</USER_REQUEST><ADDITIONAL_METADATA>The current local time is: 2026-11-28T"
        with self.full.open("wb") as stream:
            stream.write(line(row("USER_INPUT", 0, content=prompt)))
            for i in range(1, 500):
                stream.write(line(row("GENERIC", i, content="x" * (20 * 1024))))
            stream.write(line(row("USER_INPUT", 500)))
        self.compact.write_bytes(line(row("USER_INPUT", 999)))
        self.assertGreater(self.full.stat().st_size, 8 * 1024 * 1024)
        self.assertEqual(self.turn(1), (1, fingerprint(prompt)))
        self.assertEqual(self.turn(), (2, fingerprint("native-turn:500")))
        with self.full.open("ab") as stream:
            stream.write(line(row("USER_INPUT", 503)))
        from transcript_index import decode
        with patch("transcript_index.decode", wraps=decode) as parse:
            self.assertEqual(self.turn(), (3, fingerprint("native-turn:503")))
            self.assertEqual(self.turn(501), (2, fingerprint("native-turn:500")))
            self.assertEqual(parse.call_count, 1)
        self.assertNotIn(b"private sample", (self.data / "transcripts.sqlite3").read_bytes())

    def test_partial_record_then_valid_eof_then_newline(self):
        first, second = line(row("USER_INPUT", 0)), line(row("USER_INPUT", 3))
        self.full.write_bytes(first + second[:14])
        self.assertEqual(self.turn()[0], 1)
        with self.full.open("ab") as stream:
            stream.write(second[14:-1])
        self.assertEqual(self.turn()[0], 2)
        self.assertEqual(self.turn()[0], 2)
        with self.full.open("ab") as stream:
            stream.write(b"\n" + line(row("USER_INPUT", 5)))
        self.assertEqual(self.turn()[0], 3)

    def test_same_length_rewrite_replacement_and_truncation(self):
        self.full.write_bytes(line(row("USER_INPUT", 1)))
        self.assertEqual(self.turn()[1], fingerprint("native-turn:1"))
        self.full.write_bytes(line(row("USER_INPUT", 5)))
        self.assertEqual(self.turn()[1], fingerprint("native-turn:5"))
        other = self.full.with_suffix(".new")
        other.write_bytes(line(row("USER_INPUT", 6)) + line(row("USER_INPUT", 7)))
        other.replace(self.full)
        self.assertEqual(self.turn()[0], 2)
        self.full.write_bytes(line(row("USER_INPUT", 9)))
        self.assertEqual(self.turn(), (1, fingerprint("native-turn:9")))

    def test_large_line_skip_and_budget_checkpoint(self):
        self.compact.write_bytes(line(row("USER_INPUT", 333)))
        self.full.write_bytes(line(row("USER_INPUT", 0)) + line(row("GENERIC", 1, content="x" * 1200))
                              + line(row("USER_INPUT", 2)))
        with patch("transcript_index.SCAN_SECONDS", 0):
            self.assertIs(TranscriptIndex(self.data).query(self.full), INDEX_PENDING)
            self.assertIsNone(self.turn())
        with patch("transcript_index.MAX_LINE", 256), patch("transcript_index.CHUNK", 32):
            self.assertEqual(self.turn(), (2, fingerprint("native-turn:2")))

    def test_checkpoint_retains_parsed_prefix(self):
        self.full.write_bytes(line(row("USER_INPUT", 0)) + line(row("USER_INPUT", 3)))
        with patch("transcript_index.time.monotonic", side_effect=[0, 0, 1]):
            self.assertIs(TranscriptIndex(self.data).query(self.full), INDEX_PENDING)
        from transcript_index import decode
        with patch("transcript_index.decode", wraps=decode) as parse:
            self.assertEqual(self.turn(), (2, fingerprint("native-turn:3")))
            self.assertEqual(parse.call_count, 1)

    def test_compact_fallback_and_result_appended_after_planner(self):
        self.full.write_bytes(b'{"invalid":')
        self.compact.write_bytes(line(row("USER_INPUT", 0)) + line(row(
            "PLANNER_RESPONSE", 1, source="MODEL", status="DONE", tool_calls=[
                {"name": "invoke_subagent", "args": {"Subagents": '[{"TypeName": "cam-media"}]'}}])))
        self.assertEqual(self.turn()[0], 1)
        self.assertIsNone(delegated_turn(self.root, FIRST, SECOND, directory=self.data))
        with self.compact.open("ab") as stream:
            stream.write(line(row("GENERIC", 2, source="MODEL", status="DONE", content=
                                  'Created the following subagent:\n' + json.dumps({"conversationId": SECOND}))))
        self.assertEqual(delegated_turn(self.root, FIRST, SECOND, directory=self.data)[0], 1)


class MediaPolicyTests(unittest.TestCase):
    delegate = fixtures.NativeLibraryTests.delegate
    transcript = fixtures.NativeLibraryTests.transcript
    event = fixtures.NativeLibraryTests.event

    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name).resolve()
        self.data = self.root / "cache"
        from archive import Archive
        self.archive = Archive(self.data)
        self.usage = Usage(self.data)
        self.delegate()
        self.media = self.root / "clip.mp4"
        self.other = self.root / "other.mp4"
        self.media.touch()
        self.other.touch()
        (self.data / "traffic.json").write_text('{"enabled": false}', encoding="utf-8")

    def decision(self, path, cid=FIRST):
        return guard(self.event(path, conversation=cid), self.data, self.root)["decision"]

    def hook(self, phase, cid=FIRST, invocation=0, anchor=1):
        return lifecycle({"conversationId": cid, "invocationNum": invocation, "initialNumSteps": anchor}, phase,
                         self.data, self.root)

    def action(self, action, **args):
        with patch("panel.desktop_selection", return_value=("active", FIRST)), patch("panel.desktop_data", return_value=self.root):
            return run(self.archive, {"action": action, "scope": fingerprint(FIRST), **args})

    def test_mode_changes_notify_once_and_only_affect_main(self):
        self.assertEqual(self.action("policy")["mode"], "save")
        self.assertEqual(self.decision(self.media), "deny")
        self.assertIsNone(self.hook("pre"))
        self.assertEqual(self.action("set_mode", mode="native")["mode"], "native")
        self.assertIn("Native mode", self.hook("pre")["injectSteps"][0]["systemMessage"]["systemMessage"])
        self.assertIsNone(self.hook("pre"))
        self.assertEqual(self.decision(self.media), "allow")
        self.assertEqual(self.decision(self.media, SECOND), "allow")
        self.assertEqual(self.decision(self.other, SECOND), "deny")
        self.action("set_mode", mode="save")
        self.assertIn("Save mode", self.hook("pre")["injectSteps"][0]["systemMessage"]["systemMessage"])
        self.assertEqual(self.decision(self.media), "deny")

    def test_temporary_grant_is_scoped_exact_atomic_one_use(self):
        self.assertIn("error", self.action("grant", scope=fingerprint(SECOND), path=str(self.media)))
        self.assertIn("error", self.action("grant", path=self.media.name))
        self.assertIsNotNone(self.action("grant", path=str(self.media))["grantUntil"])
        self.assertIsNotNone(self.hook("pre"))
        self.assertIsNone(self.hook("pre"))
        self.assertEqual(self.decision(self.other), "deny")
        conflict = self.event(self.media)
        conflict["toolCall"]["args"]["AbsolutePath"] = str(self.other)
        self.assertEqual(guard(conflict, self.data, self.root)["decision"], "deny")
        self.assertEqual(self.decision(self.media, SECOND), "allow")  # the child's own read budget, not the grant
        self.assertIsNotNone(self.action("policy")["grantUntil"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(self.decision, [self.media, self.media])), ["allow", "deny"])
        self.assertIsNone(self.action("policy")["grantUntil"])
        self.assertIn("Save mode", self.hook("pre")["injectSteps"][0]["systemMessage"]["systemMessage"])

    def test_expiry_revoke_and_mode_switch_do_not_leak_grants(self):
        with patch("usage.time.time", return_value=100):
            self.assertEqual(self.action("grant", path=str(self.media))["grantUntil"], 400)
        with patch("usage.time.time", return_value=400):
            self.assertEqual(self.decision(self.media), "deny")
        self.action("grant", path=str(self.media))
        self.action("revoke")
        self.assertEqual(self.decision(self.media), "deny")
        self.action("grant", path=str(self.media))
        self.action("set_mode", mode="native")
        self.assertIn("error", self.action("grant", path=str(self.media)))
        self.action("set_mode", mode="save")
        self.assertEqual(self.decision(self.media), "deny")

    def test_child_gets_one_final_invocation_and_is_closed_on_post_without_metrics(self):
        self.assertIsNone(self.hook("pre", SECOND, 0, 0))
        self.assertEqual(self.decision(self.media, SECOND), "allow")
        self.assertIsNone(self.hook("pre", SECOND, 1, 3))
        self.assertIsNone(self.hook("pre", SECOND, 1, 3))  # the same hook event
        self.assertEqual(self.hook("pre", SECOND, 2, 4)["terminationBehavior"], "terminate")
        self.hook("post", SECOND, 1, 3)
        self.assertTrue(self.usage.closed(SECOND))
        self.assertEqual(self.hook("pre", SECOND, 1, 3)["terminationBehavior"], "terminate")

    def test_post_does_not_resolve_known_owner_again(self):
        key = (FIRST, 0, 1)
        with patch("usage.sample", return_value=(0, 0)):
            self.usage.begin(key, (FIRST, 1, fingerprint("turn"), "main"), Metrics())
        with patch("track_usage.owner_for", side_effect=AssertionError("unexpected lookup")):
            self.assertIsNone(self.hook("post"))
