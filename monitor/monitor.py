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

import datetime
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import boto3
import botocore.exceptions

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

# Active window — galton-stream only runs the Godot/ffmpeg/chat/music
# stack in this window. Outside it, the monitor streams the fallback
# card without escalating failures. Must match ACTIVE_*_HOUR in
# galton-stream's start.sh.
ACTIVE_TZ = ZoneInfo("America/Los_Angeles")
ACTIVE_START_HOUR = 12
ACTIVE_END_HOUR = 18

# Radio landing page (radio.dangerthirdrail.com) — S3 website bucket
# fronted by CloudFront. In-window: bucket routing rule 301-redirects to
# youtube.com/live/<video_id>. Out-of-window: routing rule removed, index.html
# (the offline title card) is served.
RADIO_BUCKET = os.environ.get("RADIO_BUCKET", "radio.dangerthirdrail.com")
RADIO_CF_DISTRIBUTION_ID = os.environ.get(
    "RADIO_CF_DISTRIBUTION_ID", "E24RTA588S2VSH"
)
RADIO_REGION = os.environ.get("RADIO_REGION", "us-east-1")
RADIO_OFFLINE_HTML_PATH = os.environ.get(
    "RADIO_OFFLINE_HTML_PATH", "/app/radio-offline.html"
)

_s3 = boto3.client("s3", region_name=RADIO_REGION)
_cf = boto3.client("cloudfront", region_name=RADIO_REGION)


def in_active_window():
    now = datetime.datetime.now(ACTIVE_TZ)
    return ACTIVE_START_HOUR <= now.hour < ACTIVE_END_HOUR

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
    """Get the most recent broadcast (any status). Used to clone metadata
    for the next day's broadcast."""
    for status in ("active", "upcoming", "completed"):
        result = youtube_api_request(
            "https://www.googleapis.com/youtube/v3/liveBroadcasts"
            f"?part=snippet,status,contentDetails&broadcastStatus={status}&maxResults=1"
        )
        if result and result.get("items"):
            return result["items"][0]
    return None


def get_live_or_pending_broadcasts():
    """Return broadcasts currently in active or upcoming state (i.e. still
    consuming a stream slot). Empty list means no broadcast exists right now."""
    broadcasts = []
    for status in ("active", "upcoming"):
        result = youtube_api_request(
            "https://www.googleapis.com/youtube/v3/liveBroadcasts"
            f"?part=snippet,status,contentDetails&broadcastStatus={status}&maxResults=10"
        )
        if result:
            broadcasts.extend(result.get("items", []))
    return broadcasts


def transition_broadcast(broadcast_id, target_status):
    """Transition a broadcast through its lifecycle. target_status: testing, live, complete."""
    result = youtube_api_request(
        "https://www.googleapis.com/youtube/v3/liveBroadcasts/transition"
        f"?part=id,status&id={broadcast_id}&broadcastStatus={target_status}",
        method="POST",
    )
    if result:
        log(f"Transitioned broadcast {broadcast_id} -> {target_status}")
        return True
    return False


def delete_broadcast(broadcast_id):
    """Delete a broadcast. Used to clean up stale scheduled-but-never-started
    broadcasts that would otherwise hijack ffmpeg's push once it connects."""
    token = get_access_token()
    if not token:
        return False
    try:
        req = urllib.request.Request(
            f"https://www.googleapis.com/youtube/v3/liveBroadcasts?id={broadcast_id}",
            method="DELETE",
            headers={"Authorization": f"Bearer {token}"},
        )
        urllib.request.urlopen(req, timeout=15)
        log(f"Deleted broadcast {broadcast_id}")
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        log(f"delete_broadcast {broadcast_id} failed ({e.code}): {body}")
        return False
    except Exception as e:
        log(f"delete_broadcast {broadcast_id} exception: {e}")
        return False


def set_broadcast_privacy(broadcast_id, privacy):
    """Set privacyStatus on a broadcast (e.g. 'private' after VOD)."""
    current = youtube_api_request(
        f"https://www.googleapis.com/youtube/v3/liveBroadcasts?part=snippet,status&id={broadcast_id}"
    )
    if not current or not current.get("items"):
        log(f"set_broadcast_privacy: could not fetch {broadcast_id}")
        return False
    item = current["items"][0]
    snippet = item.get("snippet", {})
    body = {
        "id": broadcast_id,
        "snippet": {
            "title": snippet.get("title", ""),
            "scheduledStartTime": snippet.get(
                "scheduledStartTime", "1970-01-01T00:00:00Z"
            ),
        },
        "status": {"privacyStatus": privacy},
    }
    result = youtube_api_request(
        "https://www.googleapis.com/youtube/v3/liveBroadcasts?part=snippet,status",
        method="PUT",
        body=body,
    )
    if result:
        log(f"Set broadcast {broadcast_id} privacy -> {privacy}")
        return True
    return False


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
            # DVR scrubber is distracting for a radio-style stream and
            # interacts badly with ultraLow latency (player can open at a
            # rewound position instead of the live edge).
            "enableDvr": False,
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


def radio_current_video_id():
    """Return the YouTube video id radio.dangerthirdrail.com currently
    redirects to, or None if the bucket is serving the offline page."""
    try:
        cfg = _s3.get_bucket_website(Bucket=RADIO_BUCKET)
    except botocore.exceptions.ClientError as e:
        log(f"get_bucket_website failed: {e}")
        return None
    rules = cfg.get("RoutingRules") or []
    if not rules:
        return None
    key = rules[0].get("Redirect", {}).get("ReplaceKeyWith", "")
    if key.startswith("live/"):
        return key[len("live/"):]
    return None


def _invalidate_radio():
    """Invalidate CloudFront cache so the new redirect takes effect immediately."""
    try:
        _cf.create_invalidation(
            DistributionId=RADIO_CF_DISTRIBUTION_ID,
            InvalidationBatch={
                "Paths": {"Quantity": 1, "Items": ["/*"]},
                "CallerReference": f"radio-{int(time.time())}",
            },
        )
    except Exception as e:
        log(f"CloudFront invalidation failed: {e}")


def _upload_offline_html():
    """Ensure the offline title card is the bucket's index.html."""
    if not os.path.exists(RADIO_OFFLINE_HTML_PATH):
        log(f"Offline HTML not found at {RADIO_OFFLINE_HTML_PATH}, skipping upload")
        return False
    with open(RADIO_OFFLINE_HTML_PATH, "rb") as f:
        _s3.put_object(
            Bucket=RADIO_BUCKET,
            Key="index.html",
            Body=f.read(),
            ContentType="text/html; charset=utf-8",
            CacheControl="public, max-age=60",
        )
    return True


def set_radio_online(video_id):
    """Point radio.dangerthirdrail.com at https://www.youtube.com/live/<video_id>."""
    try:
        _s3.put_bucket_website(
            Bucket=RADIO_BUCKET,
            WebsiteConfiguration={
                "IndexDocument": {"Suffix": "index.html"},
                "RoutingRules": [
                    {
                        "Redirect": {
                            "HostName": "www.youtube.com",
                            "HttpRedirectCode": "302",
                            "Protocol": "https",
                            "ReplaceKeyWith": f"live/{video_id}",
                        }
                    }
                ],
            },
        )
        _invalidate_radio()
        log(f"Radio ONLINE -> youtube.com/live/{video_id}")
        return True
    except Exception as e:
        log(f"set_radio_online failed: {e}")
        return False


def set_radio_offline():
    """Drop the routing rule so the bucket's index.html (offline title card) is served."""
    try:
        _upload_offline_html()
        _s3.put_bucket_website(
            Bucket=RADIO_BUCKET,
            WebsiteConfiguration={"IndexDocument": {"Suffix": "index.html"}},
        )
        _invalidate_radio()
        log("Radio OFFLINE -> serving index.html")
        return True
    except Exception as e:
        log(f"set_radio_offline failed: {e}")
        return False


def _broadcast_is_recent(b, max_age_min=15):
    """True if the broadcast was scheduled within the last N minutes — i.e.
    we just created it and are waiting for ffmpeg. Older than that and a
    non-live bound broadcast is stale (a dangerous stream hijacker)."""
    sst = b.get("snippet", {}).get("scheduledStartTime")
    if not sst:
        return False
    try:
        t = datetime.datetime.fromisoformat(sst.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    return (datetime.datetime.now(datetime.timezone.utc) - t) < datetime.timedelta(minutes=max_age_min)


def reconcile_broadcast():
    """Edge-triggered broadcast lifecycle. Idempotent, safe to call every poll.

    In window + live broadcast          -> make sure radio points at it.
    In window + our recent broadcast    -> wait (ffmpeg still connecting).
    In window + no live/recent          -> delete any stale bound broadcasts,
                                            clone metadata from most recent,
                                            create new + bind stable liveStream.
    Outside window + running broadcast  -> transition to complete + set private
                                            + flip radio to offline.
    Outside window + no running         -> make sure radio is offline.
    """
    in_window = in_active_window()
    actives = get_live_or_pending_broadcasts()
    live_broadcast = next(
        (
            b for b in actives
            if b.get("status", {}).get("lifeCycleStatus") in ("live", "testing")
        ),
        None,
    )

    if in_window and live_broadcast:
        vid = live_broadcast.get("id")
        if radio_current_video_id() != vid:
            set_radio_online(vid)
        return

    if in_window:
        prev = get_recent_broadcast()
        if not prev:
            log("No previous broadcast to clone metadata from; skipping create")
            return
        stream_id = get_bound_stream_id(prev)

        # If we have a recently-created broadcast bound to our stream,
        # ffmpeg is still connecting — don't spawn a duplicate.
        recent_ours = next(
            (
                b for b in actives
                if b.get("status", {}).get("lifeCycleStatus") in ("created", "ready")
                and b.get("contentDetails", {}).get("boundStreamId") == stream_id
                and _broadcast_is_recent(b)
            ),
            None,
        )
        if recent_ours:
            log(f"Waiting for ffmpeg on broadcast {recent_ours.get('id')}")
            return

        # Any stale upcoming broadcast bound to our stream will hijack
        # ffmpeg's push (YouTube auto-starts the bound one). Delete them
        # first so our fresh create+bind wins.
        for b in actives:
            life = b.get("status", {}).get("lifeCycleStatus", "")
            bound = b.get("contentDetails", {}).get("boundStreamId")
            if (
                life in ("created", "ready")
                and bound == stream_id
                and not _broadcast_is_recent(b)
            ):
                bid = b.get("id")
                log(f"Deleting stale broadcast {bid} bound to our stream")
                delete_broadcast(bid)

        new_id, new_video_id = create_new_broadcast(prev)
        if not new_id:
            log("create_new_broadcast failed")
            return
        if stream_id:
            bind_stream_to_broadcast(new_id, stream_id)
            # YouTube's enableAutoStart only fires when a stream goes
            # inactive -> active with a broadcast already bound. If ffmpeg
            # is already pushing (stream already active) at bind time, the
            # broadcast will sit in `ready` forever. Bounce ffmpeg so the
            # stream reactivates with our new broadcast bound and auto-starts.
            restart_ffmpeg()
        else:
            log("No stream bound on previous broadcast; ffmpeg will auto-bind")
        send_telegram(
            f"{PREFIX} New broadcast created: https://www.youtube.com/live/{new_video_id}"
        )
        return

    # Outside the active window: only tear down broadcasts that are
    # actually running. Scheduled-but-never-started (upcoming/created/ready)
    # broadcasts are the user's own, leave them alone.
    running = [
        b for b in actives
        if b.get("status", {}).get("lifeCycleStatus") in ("live", "testing")
    ]
    for b in running:
        bid = b.get("id")
        transition_broadcast(bid, "complete")
        set_broadcast_privacy(bid, "private")
        send_telegram(
            f"{PREFIX} Broadcast ended: https://www.youtube.com/live/{bid}"
        )
    if radio_current_video_id() is not None:
        set_radio_offline()


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

    first_iteration = True
    while True:
        if not first_iteration:
            time.sleep(POLL_INTERVAL)
        first_iteration = False

        # Lifecycle: create the day's broadcast at window open, tear it
        # down at window close, keep the radio redirect pointing at the
        # right place. Runs every poll and is idempotent.
        try:
            reconcile_broadcast()
        except Exception as e:
            log(f"reconcile_broadcast raised: {e}")

        # Outside the active window, galton-stream intentionally sleeps
        # and there is no broadcast to push to — nothing to monitor.
        if not in_active_window():
            if current_state != "SCHEDULED_OFF":
                set_state(
                    "SCHEDULED_OFF",
                    f"outside active window ({ACTIVE_START_HOUR:02d}:00-"
                    f"{ACTIVE_END_HOUR:02d}:00 {ACTIVE_TZ.key})",
                )
                consecutive_failures = 0
                chat_poller_dead_count = 0
                title_writer_dead_count = 0
            stop_fallback()
            continue

        # Entering the active window from SCHEDULED_OFF: the fallback card
        # covers galton-stream spin-up; the NORMAL-recovery branch below
        # stops it once health comes back green.
        if current_state == "SCHEDULED_OFF":
            set_state("FALLBACK_ACTIVE", "entering active window")
            consecutive_failures = 0

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
