"""Synthetic local benchmark: python tests/benchmark_hooks.py (no real traffic)."""

import json
import os
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from media_delegation import transcript_paths
from transcript_index import INDEX_PENDING, TranscriptIndex
from native_state import PLUGIN_NAME

CID = "1a111111-0000-0000-0000-111111111111"


def elapsed_ms(callback):
    start = time.perf_counter()
    result = callback()
    return (time.perf_counter() - start) * 1000, result


def main():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder).resolve()
        desktop = root / ".gemini" / "antigravity"
        directory = root / PLUGIN_NAME
        directory.mkdir()
        (directory / "traffic.json").write_text('{"enabled":false}', encoding="utf-8")
        log, _ = transcript_paths(desktop, CID)
        log.parent.mkdir(parents=True)
        filler = json.dumps({"type": "GENERIC", "content": "x" * 16384}).encode() + b"\n"
        with log.open("wb") as stream:
            stream.write(b'{"type":"USER_INPUT","step_index":0}\n')
            for _ in range(4096):
                stream.write(filler)
        with closing(sqlite3.connect(desktop / "conversation_summaries.db")) as db, db:
            db.execute("CREATE TABLE conversation_summaries (conversation_id, agent_name, parent_conversation_id)")
            db.execute("INSERT INTO conversation_summaries VALUES (?, '', NULL)", (CID,))

        def query():
            return TranscriptIndex(directory).query(log)
        cold, turn = elapsed_ms(query)
        while turn is INDEX_PENDING:
            duration, turn = elapsed_ms(query)
            cold += duration
        assert turn and turn[0] == 1
        warm = [elapsed_ms(query)[0] for _ in range(31)]
        append = []
        for step in range(1, 32):
            with log.open("ab") as stream:
                stream.write((json.dumps({"type": "USER_INPUT", "step_index": step}) + "\n").encode())
            duration, turn = elapsed_ms(query)
            assert turn[0] == step + 1
            append.append(duration)
        print(f"{log.stat().st_size / 2**20:.2f} MiB: cold index {cold:.2f} ms; "
              f"indexed lookup median {statistics.median(warm):.2f} ms; "
              f"one appended row median {statistics.median(append):.2f} ms")
        env = {**os.environ, "LOCALAPPDATA": str(root), "XDG_DATA_HOME": str(root),
               "USERPROFILE": str(root), "HOME": str(root)}
        def invocation(phase, number):
            cmd = [sys.executable, str(ROOT / "scripts" / "track_usage.py"), phase]
            payload = json.dumps({"conversationId": CID, "invocationNum": number, "initialNumSteps": 33})
            result = subprocess.run(cmd, input=payload, env=env, text=True, capture_output=True, check=True)
            assert not result.stderr and json.loads(result.stdout) == {}

        def measure(label):
            times = {"pre": [], "post": []}
            for number in range(16):
                for phase, values in times.items():
                    duration, _ = elapsed_ms(lambda: invocation(phase, number))
                    if number:
                        values.append(duration)
            print(f"{label}: pre {statistics.median(times['pre']):.2f} ms; "
                  f"post {statistics.median(times['post']):.2f} ms (process medians)")

        measure("Metrics disabled")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                assert self.path == "/debug/vars"
                payload = b'{"stats":{"outbound":{"proxy":{"uplink":100}}}}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                (directory / "traffic.json").write_text(json.dumps({"port": server.server_port}), encoding="utf-8")
                measure("Synthetic local Metrics")
            finally:
                server.shutdown()
                thread.join()

if __name__ == "__main__":
    main()
