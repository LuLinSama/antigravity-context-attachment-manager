"""Serve the local archive for Antigravity and relay requests on routed launches."""

import os
import re
import sys

from archive import Archive
from native_state import data_dir, exclude_loopback, read_state, write_state
from proxy import GOOGLE_HOSTS, RelayServer


def shortcut_listener(env):
    if env.get("AGY_CAM_SIDECAR_RELAY") != "1":
        return None
    endpoint = env.get("CLOUD_CODE_URL", "")
    match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})(/cam-[a-f0-9]{32})", endpoint)
    if match is None or not 1024 <= int(match[1]) <= 65535:
        raise ValueError("Invalid local attachment endpoint in the Antigravity shortcut.")
    return int(match[1]), match[2]


def main():
    if sys.platform != "win32" or os.environ.get("AGY_CAM_PANEL_URL"):
        return 0  # The standalone launcher already owns the relay.
    try:
        route = shortcut_listener(os.environ)
    except ValueError as error:
        print(error, file=sys.stderr, flush=True)
        return 1
    if route is not None:
        exclude_loopback()
    folder = data_dir()
    port, prefix = route or (0, None)
    with RelayServer("https://" + GOOGLE_HOSTS[0], Archive(folder), panel_only=route is None,
                     port=port, prefix=prefix) as server:
        endpoint_file = folder / "native-panel.json"
        write_state(endpoint_file, {"url": server.panel_url, "pid": os.getpid(), "routed": route is not None})
        description = "model proxy and panel" if route is not None else "panel only"
        print(f"Native sidecar ready ({description}) on 127.0.0.1:{server.server_port}.", flush=True)
        try:
            server.serve_forever()
        finally:
            if read_state(endpoint_file).get("pid") == os.getpid():
                endpoint_file.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
