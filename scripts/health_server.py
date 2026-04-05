#!/usr/bin/env python3
"""HTTP health server that replaces watchdog.sh.

Monitors FFmpeg stream health and exposes status over HTTP for
galton-monitor to poll. Also accepts restart commands.
"""

import json
import os
import signal
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

CHECK_INTERVAL = 60
STALL_THRESHOLD = 3
PREFIX = "Galton monitor:"

# Telegram (optional, secondary alerts)
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Shared state
state_lock = threading.Lock()
state = {
    "status": "starting",  # starting, alive, stalled, dead
    "tx_bytes": 0,
    "ffmpeg_pid": None,
    "stall_count": 0,
    "uptime_start": time.time(),
}


def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        data = urllib.parse.urlencode({
            "chat_id": CHAT_ID,
            "text": text,
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=data,
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass
    print(f"Telegram: {text}", file=sys.stderr, flush=True)


def get_tx_bytes():
    """Read total TX bytes from /proc/net/dev, skipping loopback."""
    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()
        total = 0
        for line in lines[2:]:  # skip headers
            if "lo:" in line:
                continue
            parts = line.split()
            if len(parts) >= 10:
                total += int(parts[9])
        return total
    except Exception:
        return 0


def find_ffmpeg_pid():
    """Find the main streaming FFmpeg PID (the one writing to RTMP)."""
    try:
        for pid_dir in os.listdir("/proc"):
            if not pid_dir.isdigit():
                continue
            try:
                with open(f"/proc/{pid_dir}/cmdline", "rb") as f:
                    cmdline = f.read().decode("utf-8", errors="replace")
                if "flv" in cmdline and "rtmp" in cmdline:
                    return int(pid_dir)
            except (FileNotFoundError, PermissionError):
                continue
    except Exception:
        pass
    return None


def watchdog_loop():
    """Background thread that monitors FFmpeg health."""
    prev_bytes = None
    alerted = False

    send_telegram(f"{PREFIX} Watchdog started, monitoring stream.")

    while True:
        time.sleep(CHECK_INTERVAL)

        ffmpeg_pid = find_ffmpeg_pid()
        tx_bytes = get_tx_bytes()

        with state_lock:
            state["tx_bytes"] = tx_bytes
            state["ffmpeg_pid"] = ffmpeg_pid

        if ffmpeg_pid is None:
            with state_lock:
                state["status"] = "dead"
                state["stall_count"] = 0
            if not alerted:
                send_telegram(f"{PREFIX} Main FFmpeg process not found! Stream is down.")
                alerted = True
            prev_bytes = None
            continue

        print(f"TX bytes: {tx_bytes} (prev: {prev_bytes})", file=sys.stderr, flush=True)

        if prev_bytes is not None and tx_bytes == prev_bytes:
            with state_lock:
                state["stall_count"] += 1
                stall_count = state["stall_count"]
            print(f"Stall check: no new bytes ({stall_count}/{STALL_THRESHOLD})",
                  file=sys.stderr, flush=True)
            if stall_count >= STALL_THRESHOLD and not alerted:
                with state_lock:
                    state["status"] = "stalled"
                send_telegram(
                    f"{PREFIX} Stream stalled -- no bytes sent in "
                    f"{STALL_THRESHOLD * CHECK_INTERVAL}s."
                )
                alerted = True
        else:
            if alerted:
                send_telegram(f"{PREFIX} Stream recovered, bytes flowing again.")
            with state_lock:
                state["status"] = "alive"
                state["stall_count"] = 0
            alerted = False

        prev_bytes = tx_bytes


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            with state_lock:
                payload = {
                    "status": state["status"],
                    "tx_bytes": state["tx_bytes"],
                    "ffmpeg_pid": state["ffmpeg_pid"],
                    "stall_count": state["stall_count"],
                    "uptime_seconds": int(time.time() - state["uptime_start"]),
                }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/restart-ffmpeg":
            pid = find_ffmpeg_pid()
            if pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                    msg = f"Killed FFmpeg PID {pid}"
                except ProcessLookupError:
                    msg = "FFmpeg already dead"
            else:
                msg = "No FFmpeg process found"
            send_telegram(f"{PREFIX} Restart requested: {msg}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"message": msg}).encode())

        elif self.path == "/restart-all":
            send_telegram(f"{PREFIX} Full restart requested.")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"message": "Restarting"}).encode())
            # Give response time to send, then exit
            threading.Timer(1.0, lambda: os._exit(1)).start()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress default access logs


class IPv6HTTPServer(HTTPServer):
    address_family = socket.AF_INET6


if __name__ == "__main__":
    # Start watchdog in background
    t = threading.Thread(target=watchdog_loop, daemon=True)
    t.start()

    # Start HTTP server (IPv6 dual-stack for Railway internal networking)
    port = int(os.environ.get("PORT", "8080"))
    server = IPv6HTTPServer(("::", port), HealthHandler)
    print(f"Health server listening on [::]:{port}", file=sys.stderr, flush=True)
    server.serve_forever()
