import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from test_native_library import FIRST, SECOND
from conversation_names import fingerprint
from track_usage import lifecycle
from usage import MAX_RESPONSE, Metrics, Usage, sample, settings


class UsageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.usage = Usage(self.root)
        self.config = Metrics()
        self.main = (FIRST, 1, fingerprint("main"), "main")
        self.child = (*self.main[:3], "child")
        self.a = (FIRST, 0, 1)
        self.b = (SECOND, 0, 1)

    def data(self):
        turn, = self.usage.turns(FIRST, [])
        return turn

    def test_nonoverlapping_windows_split_main_and_child(self):
        with patch("usage.sample", side_effect=[(10, 100), (80, 100), (85, 105), (300, 110), (305, 120), (315, 125)]):
            for key, owner in ((self.a, self.main), (self.b, self.child), ((FIRST, 1, 4), self.main)):
                self.usage.begin(key, owner, self.config)
                self.usage.begin(key, owner, self.config)  # Duplicate hook shouldn't sample twice.
                self.usage.end(key, owner, self.config)
                self.usage.end(key, owner, self.config)
        turn = self.data()
        self.assertEqual((turn["main"]["bytes"], turn["child"]["bytes"]), (80, 215))

    def test_overlapping_windows_are_not_attributed(self):
        with patch("usage.sample", return_value=(10, 50)) as read:
            self.usage.begin(self.a, self.main, self.config)
            self.usage.begin(self.b, self.child, self.config)
            self.usage.end(self.a, self.main, self.config)
            self.usage.end(self.b, self.child, self.config)
            read.assert_called_once()
        for role in ("main", "child"):
            self.assertEqual(self.data()[role], {"status": "overlap", "tag": "proxy"})

    def test_partial_bytes_are_not_reported_as_complete_role_total(self):
        with patch("usage.sample", side_effect=[(100, 500), (150, 501), (150, 510)]):
            self.usage.begin(self.a, self.main, self.config)
            self.usage.end(self.a, self.main, self.config)
            second = (FIRST, 1, 2)
            self.usage.begin(second, self.main, self.config)
            self.usage.begin(self.b, self.child, self.config)
            self.usage.end(self.b, self.child, self.config)
            self.usage.end(second, self.main, self.config)
        self.assertEqual(self.data()["main"], {"status": "overlap", "tag": "proxy", "sampledBytes": 50, "windows": 1})

    def test_counter_reset_unreachable_and_absent_start(self):
        with patch("usage.sample", side_effect=[(100, 500), (101, 50), None]):
            self.usage.begin(self.a, self.main, self.config)
            self.usage.end(self.a, self.main, self.config)
            self.usage.begin(self.b, self.child, self.config)
            self.usage.end(self.b, self.child, self.config)
        self.assertEqual((self.data()["main"]["status"], self.data()["child"]["status"]), ("reset", "unavailable"))
        self.usage.end((SECOND, 1, 2), self.child, self.config)
        self.assertEqual(self.data()["child"]["status"], "incomplete")

    def test_idle_stop_closes_missing_post_and_processing_does_not_resume_agent(self):
        event = {"conversationId": FIRST, "invocationNum": 0, "initialNumSteps": 1}
        with patch("usage.sample", return_value=(100, 500)), patch("track_usage.owner_for", return_value=self.main):
            self.assertIsNone(lifecycle(event, "pre", self.root, self.root))
            self.assertEqual(self.data()["main"]["status"], "active")
            lifecycle({**event, "fullyIdle": False}, "stop", self.root, self.root)
            self.assertEqual(self.data()["main"]["status"], "active")
            self.assertEqual(lifecycle({**event, "fullyIdle": True}, "stop", self.root, self.root), {"decision": "stop"})
            self.assertEqual(self.data()["main"]["status"], "incomplete")

    def test_settings_and_fixed_local_read_only_http(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            status, payload = 200, json.dumps({"stats": {"outbound": {"proxy": {"uplink": 234}}}}).encode()

            def do_GET(self):
                seen.append(self.path)
                self.send_response(self.status)
                self.send_header("Location", "http://127.0.0.1:1/other")
                self.send_header("Content-Length", str(len(self.payload)))
                self.end_headers()
                try:
                    self.wfile.write(self.payload)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        with server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                config_file = self.root / "traffic.json"
                config_file.write_text(json.dumps({"port": server.server_port}), encoding="utf-8")
                config = settings(self.root)
                self.assertEqual(sample(config), (234, None))
                Handler.status = 302
                self.assertIsNone(sample(config))
                Handler.status, Handler.payload = 200, b" " * (MAX_RESPONSE + 1)
                self.assertIsNone(sample(config))
                self.assertEqual(seen, ["/debug/vars"] * 3)
                config_file.write_text('{"port":"https://example.com"}', encoding="utf-8")
                self.assertIsNone(settings(self.root))
            finally:
                server.shutdown()
                thread.join()
