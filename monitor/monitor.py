#!/usr/bin/env python3
"""galton-monitor: Watches galton-stream health and YouTube live status.

Polls galton-stream's /health endpoint over Railway internal networking.
Recovery escalation:
  1 fail (120s)  → start fallback stream
  5 fails (600s) → POST /restart-all on galton-stream (container restart)
  6 fails (720s) → redeploy galton-stream via Railway API
  7 fails (840s) → alert that all recovery failed

Checks YouTube broadcast status via OAuth on every poll and state transition.
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
chat_poller_dead_count = 0
title_writer_dead_count = 0


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
        log(f"Telegram: {text}")
    except Exception as e:
        log(f"Telegram send failed: {e}")
    log(f"Telegram: {text}")


def get_access_token():
    """Get a valid OAuth access token, refreshing if needed."""
    global _access_token, _token_expires
    if not YOUTUBE_REFRESH_TOKEN or not YOUTUBE_CLIENT_ID:
        log(f"OAuth skipped: refresh_token={bool(YOUTUBE_REFRESH_TOKEN)}, client_id={bool(YOUTUBE_CLIENT_ID)}")
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


QUOTA_PROBE_INTERVAL = 600  # 10 minutes

_next_quota_probe = 0.0
_quota_last_state = None  # None | "ok" | "exhausted" | "error"


def probe_youtube_quota():
    """Cheap 1-unit broadcasts.list call. Returns 'ok', 'exhausted', or 'error'.

    Any 200 response — including 'no active broadcast' — means quota is
    live; a 403 with reason 'quotaExceeded' means the daily bucket is
    drained. Everything else is lumped into 'error'.
    """
    token = get_access_token()
    if not token:
        return "error"
    url = (
        "https://www.googleapis.com/youtube/v3/liveBroadcasts"
        "?part=id&broadcastStatus=active&broadcastType=all"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return "ok"
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()
        except Exception:
            body = ""
        if e.code == 403 and "quotaExceeded" in body:
            return "exhausted"
        log(f"Quota probe HTTP {e.code}: {body[:200]}")
        return "error"
    except Exception as e:
        log(f"Quota probe network error: {e}")
        return "error"


def maybe_probe_quota():
    """Run a quota probe on a 10-minute cadence, alerting Telegram only on
    state transitions between ok/exhausted/error.

    chat_poller retries gRPC RESOURCE_EXHAUSTED silently in a loop and the
    health server reports it alive, so without this probe quota exhaustion
    is invisible to the monitor. Cost: 144 units/day out of 10k.
    """
    global _next_quota_probe, _quota_last_state

    now = time.time()
    if now < _next_quota_probe:
        return

    _next_quota_probe = now + QUOTA_PROBE_INTERVAL

    state = probe_youtube_quota()
    log(f"[quota probe] {state}")

    if _quota_last_state is not None and state != _quota_last_state:
        send_telegram(
            f"{PREFIX} YouTube quota probe: {_quota_last_state} → {state}"
        )
    _quota_last_state = state


def check_youtube_status():
    """Check YouTube broadcast status via Data API v3. Returns (status_string, is_live)."""
    token = get_access_token()
    if not token:
        return "unknown (no OAuth token)", None

    try:
        url = "https://www.googleapis.com/youtube/v3/liveBroadcasts?part=status&broadcastStatus=active"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})

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


def youtube_api_request(url, method="GET", body=None):
    """Make an authenticated YouTube API request. Returns parsed JSON or None."""
    token = get_access_token()
    if not token:
        log("No access token for YouTube API request")
        return None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    else:
        data = None
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        log(f"YouTube API error ({e.code}): {error_body}")
        return None
    except Exception as e:
        log(f"YouTube API request failed: {e}")
        return None


def get_recent_broadcast():
    """Get the most recent broadcast (active or complete). Returns broadcast resource or None."""
    # Try active first
    for status in ("active", "completed"):
        result = youtube_api_request(
            "https://www.googleapis.com/youtube/v3/liveBroadcasts"
            f"?part=snippet,status,contentDetails&broadcastStatus={status}&maxResults=1"
        )
        if result and result.get("items"):
            return result["items"][0]
    return None


def get_bound_stream_id(broadcast):
    """Get the stream ID bound to a broadcast."""
    content_details = broadcast.get("contentDetails", {})
    return content_details.get("boundStreamId")


def create_new_broadcast(old_broadcast):
    """Create a new broadcast cloning settings from the old one.
    Returns (new_broadcast_id, new_video_id) or (None, None)."""
    snippet = old_broadcast.get("snippet", {})
    title = snippet.get("title", "Galton Board Live Stream")
    description = snippet.get("description", "")

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "scheduledStartTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
        "contentDetails": {
            "enableAutoStart": True,
            "enableAutoStop": False,
            "latencyPreference": "ultraLow",
        },
    }

    result = youtube_api_request(
        "https://www.googleapis.com/youtube/v3/liveBroadcasts?part=snippet,status,contentDetails",
        method="POST",
        body=body,
    )
    if not result:
        return None, None

    new_id = result.get("id")
    log(f"Created new broadcast: {new_id}")
    return new_id, new_id  # broadcast ID is also the video ID


def bind_stream_to_broadcast(broadcast_id, stream_id):
    """Bind an existing stream to a new broadcast."""
    result = youtube_api_request(
        "https://www.googleapis.com/youtube/v3/liveBroadcasts/bind"
        f"?part=id,contentDetails&id={broadcast_id}&streamId={stream_id}",
        method="POST",
    )
    if result:
        log(f"Bound stream {stream_id} to broadcast {broadcast_id}")
        return True
    return False


def update_broadcast_description(broadcast_id, new_description):
    """Update a broadcast's description."""
    result = youtube_api_request(
        "https://www.googleapis.com/youtube/v3/liveBroadcasts?part=snippet",
        method="PUT",
        body={
            "id": broadcast_id,
            "snippet": {
                "title": "Galton Board Live Stream",  # required by API even for description update
                "description": new_description,
                "scheduledStartTime": "1970-01-01T00:00:00Z",  # required but ignored for complete
            },
        },
    )
    return result is not None


def handle_youtube_broadcast_ended():
    """Detect ended broadcast, create new one, update old description.
    Returns True if a new broadcast was created."""
    old_broadcast = get_recent_broadcast()
    if not old_broadcast:
        log("No recent broadcast found")
        return False

    old_status = old_broadcast.get("status", {}).get("lifeCycleStatus", "")
    old_id = old_broadcast.get("id", "")

    if old_status not in ("complete", "revoked"):
        log(f"Broadcast {old_id} status is {old_status}, not ended")
        return False

    log(f"Broadcast {old_id} has ended ({old_status}). Creating replacement...")

    # Get the stream bound to the old broadcast so we can reuse it
    stream_id = get_bound_stream_id(old_broadcast)

    # Create new broadcast with same settings
    new_id, new_video_id = create_new_broadcast(old_broadcast)
    if not new_id:
        log("Failed to create new broadcast")
        return False

    # Bind the same stream to the new broadcast
    if stream_id:
        if not bind_stream_to_broadcast(new_id, stream_id):
            log(f"Failed to bind stream {stream_id} to new broadcast {new_id}")
    else:
        log("No stream ID found on old broadcast — FFmpeg will auto-bind on connect")

    # Update old broadcast description to point to new one
    old_snippet = old_broadcast.get("snippet", {})
    old_desc = old_snippet.get("description", "")
    redirect_line = f"\n\nWatch Live! https://www.youtube.com/live/{new_video_id}"
    update_broadcast_description(old_id, old_desc + redirect_line)

    msg = (
        f"{PREFIX} YouTube broadcast ended. Created new broadcast: "
        f"https://www.youtube.com/live/{new_video_id} "
        f"(old: https://www.youtube.com/live/{old_id})"
    )
    send_telegram(msg)

    return True


def on_state_transition(old_state, new_state, reason):
    """Called on every state transition."""
    msg = f"{PREFIX} State: {old_state} -> {new_state}. Reason: {reason}"
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


def restart_chat_poller():
    """POST /restart-chat-poller to galton-stream's health server."""
    try:
        req = urllib.request.Request(
            f"{GALTON_STREAM_URL}/restart-chat-poller",
            data=b"",
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=10)
        log(f"restart-chat-poller response: {resp.read().decode()}")
        return True
    except Exception as e:
        log(f"restart-chat-poller failed: {e}")
        return False


def restart_title_writer():
    """POST /restart-title-writer to galton-stream's health server."""
    try:
        req = urllib.request.Request(
            f"{GALTON_STREAM_URL}/restart-title-writer",
            data=b"",
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=10)
        log(f"restart-title-writer response: {resp.read().decode()}")
        return True
    except Exception as e:
        log(f"restart-title-writer failed: {e}")
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
    global current_state, consecutive_failures, chat_poller_dead_count, title_writer_dead_count

    log(f"Starting monitor, polling {GALTON_STREAM_URL} every {POLL_INTERVAL}s")
    log(f"YouTube OAuth: client_id={'set' if YOUTUBE_CLIENT_ID else 'MISSING'}, "
        f"client_secret={'set' if YOUTUBE_CLIENT_SECRET else 'MISSING'}, "
        f"refresh_token={'set' if YOUTUBE_REFRESH_TOKEN else 'MISSING'}")
    send_telegram(f"{PREFIX} Monitor service started.")

    while True:
        time.sleep(POLL_INTERVAL)

        maybe_probe_quota()

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
        else:
            tx = health.get("tx_bytes", 0)
            consecutive_failures = 0

            # Check chat_poller health
            chat_status = health.get("chat_poller_status", "unknown")
            if chat_status == "dead":
                chat_poller_dead_count += 1
                if chat_poller_dead_count == 1:
                    log("Chat poller is dead, restarting...")
                    restart_chat_poller()
                    send_telegram(f"{PREFIX} Chat poller was dead, restarted.")
                elif chat_poller_dead_count % 5 == 0:
                    log(f"Chat poller still dead after {chat_poller_dead_count} checks, retrying...")
                    restart_chat_poller()
            else:
                if chat_poller_dead_count > 0:
                    log("Chat poller recovered")
                chat_poller_dead_count = 0

            # Check title_writer health
            title_status = health.get("title_writer_status", "unknown")
            if title_status == "dead":
                title_writer_dead_count += 1
                if title_writer_dead_count == 1:
                    log("Title writer is dead, restarting...")
                    restart_title_writer()
                    send_telegram(f"{PREFIX} Title writer was dead, restarted.")
                elif title_writer_dead_count % 5 == 0:
                    log(f"Title writer still dead after {title_writer_dead_count} checks, retrying...")
                    restart_title_writer()
            else:
                if title_writer_dead_count > 0:
                    log("Title writer recovered")
                title_writer_dead_count = 0

            log(f"Healthy: tx_bytes={tx}, uptime={health.get('uptime_seconds', 0)}s, chat={chat_status}, title={title_status}")


if __name__ == "__main__":
    main()
