#!/usr/bin/env python3
"""Token-authed /health for the box (pre-baked playout). GET /health with
`Authorization: Bearer $BOX_HEALTH_TOKEN` -> 200 + {playout_alive, ffmpeg_alive,
heartbeat_age_s}; missing/invalid/empty token -> 401. Binds 127.0.0.1:$BOX_HEALTH_PORT
(reached only via the cloudflared tunnel). Operational window mirrors monitor/windows.py."""
import datetime
import http.server
import json
import os
import subprocess
import time
from zoneinfo import ZoneInfo

HEARTBEAT = os.environ.get("PLAYOUT_HEARTBEAT", "/tmp/playout_heartbeat")
TOKEN = os.environ.get("BOX_HEALTH_TOKEN", "")
PORT = int(os.environ.get("BOX_HEALTH_PORT", "8088"))
FRESH_MAX_AGE = float(os.environ.get("PLAYOUT_HEARTBEAT_MAX_AGE", "30"))
ACTIVE_TZ = ZoneInfo("America/Los_Angeles")


def _ffmpeg_alive():
    try:
        return subprocess.run(["pgrep", "-x", "ffmpeg"], capture_output=True).returncode == 0
    except Exception:
        return False


def _heartbeat_age_s():
    try:
        return int(time.time() - os.path.getmtime(HEARTBEAT))
    except OSError:
        return -1


def _in_window(now=None):
    now = now or datetime.datetime.now(ACTIVE_TZ)
    return (11, 45) <= (now.hour, now.minute) < (18, 5)


def health_payload(ffmpeg_alive_fn=_ffmpeg_alive, heartbeat_age_fn=_heartbeat_age_s,
                   in_window_fn=_in_window):
    ff = ffmpeg_alive_fn()
    age = heartbeat_age_fn()
    fresh = 0 <= age < FRESH_MAX_AGE
    # playout_alive: doing its job — streaming-and-fresh in-window, or idle out-of-window.
    playout_alive = (ff and fresh) if in_window_fn() else True
    return {"playout_alive": playout_alive, "ffmpeg_alive": ff, "heartbeat_age_s": age}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_response(404); self.end_headers(); return
        if not TOKEN or self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.send_response(401); self.end_headers(); return
        body = json.dumps(health_payload()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    http.server.HTTPServer(("127.0.0.1", PORT), H).serve_forever()
