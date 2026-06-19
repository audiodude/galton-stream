"""YouTube Data API v3: OAuth, authenticated transport, ownership helpers, and
broadcast/stream lifecycle ops — every mutating op scoped to the owned stream."""
import datetime
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

YOUTUBE_CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
YOUTUBE_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")
YOUTUBE_STREAM_KEY = os.environ.get("YOUTUBE_STREAM_KEY", "")

BROADCAST_TITLE = os.environ.get("BROADCAST_TITLE", "Danger Third Rail radio")
BROADCAST_DESCRIPTION = os.environ.get("BROADCAST_DESCRIPTION", "")
BROADCAST_PRIVACY = os.environ.get("BROADCAST_PRIVACY", "public")

_access_token = None
_token_expires = 0
_owned_stream_id = None

API = "https://www.googleapis.com/youtube/v3"


def _log(msg):
    import sys
    print(f"[monitor] {msg}", file=sys.stderr, flush=True)


def get_access_token():
    global _access_token, _token_expires
    if not YOUTUBE_REFRESH_TOKEN or not YOUTUBE_CLIENT_ID:
        _log(f"OAuth skipped: refresh_token={bool(YOUTUBE_REFRESH_TOKEN)}, client_id={bool(YOUTUBE_CLIENT_ID)}")
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
        with urllib.request.urlopen(req, timeout=10) as resp:
            tokens = json.loads(resp.read().decode())
        _access_token = tokens["access_token"]
        _token_expires = time.time() + tokens.get("expires_in", 3600)
        return _access_token
    except Exception as e:
        _log(f"OAuth token refresh failed: {e}")
        return None


def api(url, method="GET", body=None):
    """Authenticated request. Returns parsed JSON dict, or None on any error."""
    token = get_access_token()
    if not token:
        _log("No access token for YouTube API request")
        return None
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        _log(f"YouTube API error ({e.code}): {e.read().decode()}")
        return None
    except Exception as e:
        _log(f"YouTube API request failed: {e}")
        return None


# --- pure helpers (the ownership lens) ---

def life(b):
    return b.get("status", {}).get("lifeCycleStatus", "")


def owned_broadcasts(broadcasts, stream_id):
    return [b for b in broadcasts
            if b.get("contentDetails", {}).get("boundStreamId") == stream_id]


def is_recent(b, now, max_age_min=15):
    sst = b.get("snippet", {}).get("scheduledStartTime")
    if not sst:
        return False
    try:
        t = datetime.datetime.fromisoformat(sst.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    return (now - t) < datetime.timedelta(minutes=max_age_min)
