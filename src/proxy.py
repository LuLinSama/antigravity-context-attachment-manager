"""Loopback relay for Antigravity's Cloud Code JSON requests."""

import argparse
import gzip
import io
import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import panel
from adapter import Report, declared_tools, obj, request_payload, rewrite, scope_of
from archive import Archive, json_bytes
from native_state import app_data
from protocol import FETCH_TOOL, tool_matches


GOOGLE_HOSTS = ("daily-cloudcode-pa.googleapis.com", "cloudcode-pa.googleapis.com")
MODEL_PATH = re.compile(r":(?:streamGenerateContent|generateContent|countTokens)$")
ROUTE = re.compile(r"/v[0-9]+(?:internal|alpha|beta)?(?:[:/][a-zA-Z0-9_./:%-]+)?$")
MAX_BODY = 512 * 1024 * 1024
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RelayServer(ThreadingHTTPServer):
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, upstream, archive, observe=False, panel_only=False, port=0, prefix=None):
        self.upstream = upstream
        self.archive = archive
        self.observe = observe
        self.panel_only = panel_only
        self.prefix = prefix or "/cam-" + secrets.token_urlsafe(24)
        self.panel_path = "/panel-" + secrets.token_urlsafe(24)
        self.request_timeout = 240
        self.opener = urllib.request.build_opener(NoRedirect())
        super().__init__(("127.0.0.1", port), RelayHandler)

    def server_bind(self):
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server_port}{self.prefix}"

    @property
    def panel_url(self):
        return f"http://127.0.0.1:{self.server_port}{self.panel_path}"


def without_hop(headers):
    connection = {item.strip().lower() for item in headers.get("connection", "").split(",")}
    return [(key, value) for key, value in headers.items() if key.lower() not in HOP_HEADERS | connection]


class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        self.request.settimeout(self.server.request_timeout)
        super().setup()

    def log_message(self, format, *args):
        pass

    def handle_expect_100(self):
        if self.authorized_path() is None and not self.panel_origin():
            self.problem(HTTPStatus.NOT_FOUND, "Unknown local endpoint")
            return False
        return super().handle_expect_100()

    def panel_origin(self):
        origin = self.headers.get("Origin", "")
        expected_host = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected_host or not secrets.compare_digest(self.path.encode(), self.server.panel_path.encode()):
            return None
        if origin == "null" or re.fullmatch(r"https://(?:[a-z0-9-]+\.scf\.usercontent\.goog|127\.0\.0\.1:\d{1,5})", origin):
            return origin
        return None

    def handle_panel(self):
        origin = self.panel_origin()
        if not origin or self.command not in {"POST", "OPTIONS"}:
            self.problem(HTTPStatus.NOT_FOUND, "Unknown local endpoint")
            return
        payload = b""
        if self.command == "POST":
            length = self.headers.get("Content-Length", "")
            if self.headers.get("Transfer-Encoding") or not re.fullmatch(r"[0-9]{1,5}", length) or int(length) > 16_384:
                self.problem(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Invalid panel body size")
                return
            if self.headers.get("Content-Type", "").split(";")[0].lower() != "application/json":
                self.problem(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Expected JSON")
                return
            try:
                arguments = obj(json.loads(self.rfile.read(int(length))))
            except (ValueError, RecursionError):
                self.problem(HTTPStatus.BAD_REQUEST, "Invalid panel JSON")
                return
            payload = json_bytes(panel.run(self.server.archive, arguments, routed=not self.server.panel_only))
        self.send_response_only(HTTPStatus.OK if payload else HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "POST")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Vary", "Origin")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def authorized_path(self):
        if self.server.panel_only:
            return None
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}" or any(key in self.headers for key in ("Origin", "Sec-Fetch-Site")):
            return None
        if not self.path.startswith(self.server.prefix + "/"):
            return None
        suffix = self.path[len(self.server.prefix):]
        path = suffix.partition("?")[0]
        if not ROUTE.fullmatch(path) or any(part in {".", ".."} for part in path.split("/")):
            return None
        return suffix

    def problem(self, status, message):
        self.close_connection = True
        self.send_error(status, message)

    def read_body(self):
        transfer = self.headers.get("Transfer-Encoding", "").lower()
        lengths = self.headers.get_all("Content-Length", [])
        if transfer:
            if transfer != "chunked" or lengths:
                raise ValueError("Unsupported request transfer encoding")
            result = io.BytesIO()
            while True:
                line = self.rfile.readline(4096)
                size = line.split(b";", 1)[0].strip()
                if not line.endswith(b"\r\n") or not re.fullmatch(rb"[a-fA-F0-9]{1,12}", size):
                    raise ValueError("Malformed request chunk")
                length = int(size, 16)
                if length == 0:
                    for _ in range(100):
                        trailer = self.rfile.readline(8192)
                        if trailer == b"\r\n":
                            return result.getvalue()
                        if not trailer.endswith(b"\r\n"):
                            break
                    raise ValueError("Malformed request trailers")
                if result.tell() + length > MAX_BODY:
                    raise OverflowError
                chunk = self.rfile.read(length)
                if len(chunk) != length or self.rfile.read(2) != b"\r\n":
                    raise ValueError("Truncated request chunk")
                result.write(chunk)
        if not lengths:
            return b""
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,12}", lengths[0]):
            raise ValueError("Invalid request length")
        length = int(lengths[0])
        if length > MAX_BODY:
            raise OverflowError
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("Truncated request body")
        return body

    def transform(self, data, report):
        encoding = self.headers.get("Content-Encoding", "identity").lower().strip()
        content_type = self.headers.get("Content-Type", "application/json").split(";", 1)[0].strip().lower()
        if encoding not in {"identity", "gzip"} or content_type != "application/json":
            report.state = "unsupported-encoding-or-content-type"
            return data
        try:
            if encoding == "gzip":
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as source:
                    decoded = source.read(MAX_BODY + 1)
                if len(decoded) > MAX_BODY:
                    raise OverflowError
            else:
                decoded = data
            body = obj(json.loads(decoded))
        except (ValueError, OSError, EOFError, RecursionError):
            report.state = "invalid-json"
            return data
        headers = {key.lower(): value for key, value in self.headers.items()}
        if self.server.observe:
            request = request_payload(body)
            _, report.scope_field = scope_of(body, request, headers)
            available = any(tool_matches(name, FETCH_TOOL) for name in declared_tools(request))
            report.state = "observe-tool-present" if available else "observe-tool-absent"
            return data
        rewrite(body, headers, self.server.archive, report)
        if not report.changed:
            return data
        encoded = json_bytes(body)
        return gzip.compress(encoded, compresslevel=3, mtime=0) if encoding == "gzip" else encoded

    def relay(self):
        if self.path.startswith("/panel-"):
            self.handle_panel()
            return
        target = self.authorized_path()
        if target is None:
            self.problem(HTTPStatus.NOT_FOUND, "Unknown local endpoint")
            return
        model = self.command == "POST" and MODEL_PATH.search(urlsplit(target).path) is not None
        report = Report()
        try:
            data = self.read_body()
            incoming_size = len(data)
            if model:
                data = self.transform(data, report)
        except OverflowError:
            self.problem(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Model request exceeds the local relay limit")
            return
        except (ValueError, TimeoutError):
            self.problem(HTTPStatus.BAD_REQUEST, "Invalid local relay request")
            return
        except Exception as error:
            print(f"Attachment rewrite failed ({type(error).__name__}); request was not forwarded.", file=sys.stderr, flush=True)
            self.problem(HTTPStatus.INSUFFICIENT_STORAGE, "Local attachment rewrite failed")
            return

        headers = {key: value for key, value in without_hop(self.headers) if key.lower() not in {"host", "content-length", "expect"}}
        request = urllib.request.Request(self.server.upstream + target, data=data if data or self.command in {"POST", "PUT", "PATCH"} else None, headers=headers, method=self.command)
        status = None
        try:
            try:
                upstream_response = self.server.opener.open(request, timeout=self.server.request_timeout)
            except urllib.error.HTTPError as error:
                upstream_response = error
            except (urllib.error.URLError, OSError):
                self.problem(HTTPStatus.BAD_GATEWAY, "Could not connect to the Google endpoint")
                return
            with upstream_response as response:
                status = response.code
                if report.scope and 200 <= status < 300 and ":countTokens" not in target:
                    try:
                        self.server.archive.record_forward(report.scope, incoming_size, len(data), report)
                    except (OSError, sqlite3.Error) as error:
                        print(f"Could not record attachment statistics ({type(error).__name__}).", file=sys.stderr, flush=True)
                self.send_response_only(status)
                for key, value in without_hop(response.headers):
                    self.send_header(key, value)
                self.close_connection = True
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    while block := response.read1(64 * 1024):
                        self.wfile.write(block)
        except OSError:
            self.close_connection = True
        finally:
            if model:
                print(json.dumps({
                    "http": status,
                    "mode": report.state,
                    "scopeField": report.scope_field,
                    "receivedBytes": incoming_size,
                    "forwardedBytes": len(data),
                    "mediaSeen": report.seen,
                    "omitted": report.omitted,
                    "restored": report.restored,
                    "listed": report.listed,
                    "deferred": report.deferred,
                }), flush=True)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = relay
    do_OPTIONS = handle_panel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True, help="Antigravity executable (App or agy CLI)")
    parser.add_argument("--data-dir", type=Path, default=app_data() / "antigravity-context-attachment-manager")
    parser.add_argument("--google-host", choices=GOOGLE_HOSTS, default=GOOGLE_HOSTS[0])
    parser.add_argument("--observe", action="store_true", help="Log only request sizes and protocol capabilities. Forward bodies unchanged.")
    parser.add_argument("app_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    app_args = args.app_args[1:] if args.app_args[0:1] == ["--"] else args.app_args
    return launch(args.app, app_args, args.data_dir, args.google_host, args.observe)


@contextmanager
def start_relay(google_host, archive, observe=False):
    with RelayServer("https://" + google_host, archive, observe) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server
        finally:
            server.shutdown()
            worker.join()


def launch(app, app_args, data_dir, google_host=GOOGLE_HOSTS[0], observe=False):
    app = app.resolve(strict=True)
    archive = None if observe else Archive(data_dir.resolve())
    with start_relay(google_host, archive, observe) as server:
        env = dict(os.environ, CLOUD_CODE_URL=server.base_url, AGY_CAM_PANEL_URL=server.panel_url)
        print(f"Local attachment relay is ready on 127.0.0.1:{server.server_port}. Start a fresh Antigravity conversation to verify it.", flush=True)
        child = subprocess.Popen([str(app), *app_args], env=env)
        print(f"Antigravity process {child.pid} launched.", flush=True)
        start = time.monotonic()
        code = child.wait()
        if time.monotonic() - start < 3:
            print("Antigravity exited immediately. Close any existing Antigravity instance before using the launcher.", file=sys.stderr, flush=True)
        return code


if __name__ == "__main__":
    raise SystemExit(main())
