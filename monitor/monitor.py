#!/usr/bin/env python3
"""galton-monitor: Watches galton-stream health and YouTube live status.

Polls galton-stream's /health endpoint over Railway internal networking.
Recovery escalation:
  1 fail (120s)  → start fallback stream
  5 fails (600s) → POST /restart-all on galton-stream (container restart)
  6 fails (720s) → redeploy galton-stream via Railway API
  7 fails (840s) → alert that all recovery failed

On every state transition, checks YouTube broadcast status.
"""

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# Configuration
GALTON_STREAM_URL = os.environ.get(
    "GALTON_STREAM_URL", "http://galton-stream.railway.internal:8080"
)
YOUTUBE_STREAM_KEY = os.environ.get("YOUTUBE_STREAM_KEY", "")
YOUTUBE_URL = "rtmp://a.rtmp.youtube.com/live2"
BACKUP_IMAGE = "/app/backup.png"
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
POLL_INTERVAL = 120

# Railway API for service redeploy
RAILWAY_API_TOKEN = os.environ.get("RAILWAY_API_TOKEN", "")
GALTON_STREAM_SERVICE_ID = os.environ.get("GALTON_STREAM_SERVICE_ID", "")
GALTON_STREAM_ENVIRONMENT_ID = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")

# YouTube Data API (optional, for broadcast status checks)
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")

# YouTube OAuth (for authenticated broadcast status)
YOUTUBE_CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
YOUTUBE_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")

# Cached OAuth access token
_access_token = None
_token_expires = 0

PREFIX = "Galton monitor:"

# State
fallback_proc = None
current_state = "STARTING"  # NORMAL, FALLBACK_ACTIVE, RESTARTED_ALL, RESTARTED_RAILWAY, DEAD
consecutive_failures = 0
youtube_failures = 0


def log(msg):
    print(f"[monitor] {msg}", file=sys.stderr, flush=True)


def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        log(f"(no telegram) {text}")
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
    except Exception as e:
        log(f"Telegram send failed: {e}")
    log(f"Telegram: {text}")


def get_access_token():
    """Get a valid OAuth access token, refreshing if needed."""
    global _access_token, _token_expires
    if not YOUTUBE_REFRESH_TOKEN or not YOUTUBE_CLIENT_ID:
        return None
    if _access_token and time.time() < _token_expires - 60:
        return _access_token
    try:
        data = urllib.parse.urlencode({
            "client_id": YOUTUBE_CLIENT_ID,
            "client_secret": YOUTUBE_CLIENT_SECRET,
            "refresh_token": YOUTUBE_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
        resp = urllib.request.urlopen(req, timeout=10)
        tokens = json.loads(resp.read().decode())
        _access_token = tokens["access_token"]
        _token_expires = time.time() + tokens.get("expires_in", 3600)
        return _access_token
    except Exception as e:
        log(f"OAuth token refresh failed: {e}")
        return None


def check_youtube_status():
    """Check YouTube broadcast status via Data API v3. Returns (status_string, is_live)."""
    token = get_access_token()
    if not token and not YOUTUBE_API_KEY:
        return "unknown (no credentials)", None

    try:
        if token:
            # Authenticated: can see our own broadcasts
            url = "https://www.googleapis.com/youtube/v3/liveBroadcasts?part=status&broadcastStatus=active"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        else:
            url = (
                "https://www.googleapis.com/youtube/v3/liveBroadcasts"
                f"?part=status&broadcastStatus=active&key={YOUTUBE_API_KEY}"
            )
            req = urllib.request.Request(url)

        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode())
        items = data.get("items", [])
        if not items:
            return "no active broadcast", False
        status = items[0].get("status", {})
        life = status.get("lifeCycleStatus", "unknown")
        recording = status.get("recordingStatus", "unknown")
        is_live = life == "live" and recording == "recording"
        return f"{life}/{recording}", is_live
    except Exception as e:
        log(f"YouTube API check failed: {e}")
        return f"error: {e}", None


def on_state_transition(old_state, new_state, reason):
    """Called on every state transition. Checks YouTube and alerts."""
    yt_status, _ = check_youtube_status()
    msg = (
        f"{PREFIX} State: {old_state} -> {new_state}. "
        f"Reason: {reason}. YouTube: {yt_status}"
    )
    send_telegram(msg)
    log(msg)


def poll_health():
    """Poll galton-stream's health endpoint. Returns dict or None on failure."""
    try:
        req = urllib.request.Request(f"{GALTON_STREAM_URL}/health")
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read().decode())
    except Exception:
        return None


def start_fallback():
    """Start fallback FFmpeg streaming backup image to YouTube."""
    global fallback_proc

    if fallback_proc and fallback_proc.poll() is None:
        return  # already running

    if not os.path.exists(BACKUP_IMAGE):
        log(f"Cannot start fallback: {BACKUP_IMAGE} not found")
        return
    if not YOUTUBE_STREAM_KEY:
        log("Cannot start fallback: YOUTUBE_STREAM_KEY not set")
        return

    log("Starting fallback stream...")
    fallback_proc = subprocess.Popen([
        "ffmpeg", "-loglevel", "warning",
        "-loop", "1", "-re", "-i", BACKUP_IMAGE,
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
        "-b:v", "500k", "-maxrate", "500k", "-bufsize", "1000k",
        "-pix_fmt", "yuv420p", "-g", "60",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-shortest", "-f", "flv",
        f"{YOUTUBE_URL}/{YOUTUBE_STREAM_KEY}",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    log(f"Fallback stream started (PID {fallback_proc.pid})")


def stop_fallback():
    """Stop fallback FFmpeg if running."""
    global fallback_proc

    if fallback_proc is None:
        return
    if fallback_proc.poll() is not None:
        fallback_proc = None
        return

    log(f"Stopping fallback stream (PID {fallback_proc.pid})...")
    fallback_proc.terminate()
    try:
        fallback_proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        fallback_proc.kill()
        fallback_proc.wait()
    fallback_proc = None
    log("Fallback stream stopped")


def restart_ffmpeg():
    """POST /restart-ffmpeg to galton-stream's health server."""
    try:
        req = urllib.request.Request(
            f"{GALTON_STREAM_URL}/restart-ffmpeg",
            data=b"",
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=10)
        log(f"restart-ffmpeg response: {resp.read().decode()}")
        return True
    except Exception as e:
        log(f"restart-ffmpeg failed: {e}")
        return False


def restart_galton_stream():
    """POST /restart-all to galton-stream's health server."""
    try:
        req = urllib.request.Request(
            f"{GALTON_STREAM_URL}/restart-all",
            data=b"",
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=10)
        log(f"restart-all response: {resp.read().decode()}")
        return True
    except Exception as e:
        log(f"restart-all failed: {e}")
        return False


def redeploy_railway():
    """Redeploy galton-stream via Railway GraphQL API."""
    if not RAILWAY_API_TOKEN or not GALTON_STREAM_SERVICE_ID:
        log("Cannot redeploy: RAILWAY_API_TOKEN or RAILWAY_SERVICE_ID not set")
        return False

    query = """
    mutation serviceInstanceRedeploy($serviceId: String!, $environmentId: String!) {
        serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
    }
    """
    variables = {
        "serviceId": GALTON_STREAM_SERVICE_ID,
        "environmentId": GALTON_STREAM_ENVIRONMENT_ID,
    }
    payload = json.dumps({"query": query, "variables": variables}).encode()

    try:
        req = urllib.request.Request(
            "https://backboard.railway.com/graphql/v2",
            data=payload,
            headers={
                "Authorization": f"Bearer {RAILWAY_API_TOKEN}",
                "Content-Type": "application/json",
            },
        )
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read().decode())
        log(f"Railway redeploy response: {result}")
        if "errors" in result:
            log(f"Railway API errors: {result['errors']}")
            return False
        return True
    except Exception as e:
        log(f"Railway redeploy failed: {e}")
        return False


def set_state(new_state, reason):
    global current_state
    if new_state != current_state:
        on_state_transition(current_state, new_state, reason)
        current_state = new_state


def main():
    global current_state, consecutive_failures, youtube_failures

    log(f"Starting monitor, polling {GALTON_STREAM_URL} every {POLL_INTERVAL}s")
    send_telegram(f"{PREFIX} Monitor service started.")

    while True:
        time.sleep(POLL_INTERVAL)

        # Keep fallback alive if it should be running
        if current_state in ("FALLBACK_ACTIVE", "RESTARTED_ALL", "RESTARTED_RAILWAY", "DEAD"):
            if fallback_proc and fallback_proc.poll() is not None:
                log("Fallback FFmpeg died, restarting...")
                start_fallback()

        health = poll_health()

        if health is None or health.get("status") in ("dead", "stalled"):
            consecutive_failures += 1
            status_detail = "unreachable" if health is None else health.get("status")
            log(f"Failure #{consecutive_failures}: {status_detail}")

            if consecutive_failures == 1:
                # First failure → start fallback immediately
                start_fallback()
                set_state("FALLBACK_ACTIVE", f"stream {status_detail}")

            elif consecutive_failures == 5:
                # 600s of failure → restart the container
                log("5 failures, requesting container restart...")
                restart_galton_stream()
                set_state("RESTARTED_ALL", "5 consecutive failures (600s)")

            elif consecutive_failures == 6:
                # 720s → redeploy via Railway API
                log("6 failures, redeploying via Railway...")
                if redeploy_railway():
                    set_state("RESTARTED_RAILWAY", "6 consecutive failures, Railway redeploy")
                else:
                    set_state("RESTARTED_RAILWAY", "6 failures, Railway redeploy FAILED")

            elif consecutive_failures == 7:
                # 840s → all recovery exhausted
                set_state("DEAD", "all recovery attempts exhausted after 840s")

            continue

        # galton-stream is alive and healthy
        if current_state != "NORMAL":
            stop_fallback()
            set_state("NORMAL", "stream recovered")
            consecutive_failures = 0
            youtube_failures = 0
        else:
            tx = health.get("tx_bytes", 0)
            consecutive_failures = 0

            # Check if YouTube broadcast is still alive
            yt_status, yt_live = check_youtube_status()
            if yt_live is False:
                youtube_failures += 1
                log(f"YouTube down ({yt_status}), yt_fail #{youtube_failures}")
                if youtube_failures == 2:
                    # Two consecutive checks (240s) with no broadcast — restart FFmpeg
                    # so it reconnects to YouTube RTMP
                    msg = (f"{PREFIX} YouTube broadcast down ({yt_status}) but "
                           f"galton-stream healthy. Restarting FFmpeg to reconnect.")
                    send_telegram(msg)
                    log(msg)
                    restart_ffmpeg()
                elif youtube_failures == 4:
                    # 480s — full container restart
                    msg = (f"{PREFIX} YouTube still down after FFmpeg restart. "
                           f"Restarting container.")
                    send_telegram(msg)
                    log(msg)
                    restart_galton_stream()
                    youtube_failures = 0
            else:
                if youtube_failures > 0:
                    log(f"YouTube recovered ({yt_status})")
                youtube_failures = 0

            log(f"Healthy: tx_bytes={tx}, uptime={health.get('uptime_seconds', 0)}s, yt={yt_status}")


if __name__ == "__main__":
    main()
