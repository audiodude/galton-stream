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


# --- lifecycle ops ---

def get_owned_stream_id():
    global _owned_stream_id
    if _owned_stream_id:
        return _owned_stream_id
    if not YOUTUBE_STREAM_KEY:
        _log("YOUTUBE_STREAM_KEY unset; cannot resolve owned stream")
        return None
    result = api(f"{API}/liveStreams?part=id,cdn&mine=true&maxResults=50")
    if not result:
        return None
    for s in result.get("items", []):
        if s.get("cdn", {}).get("ingestionInfo", {}).get("streamName") == YOUTUBE_STREAM_KEY:
            _owned_stream_id = s.get("id")
            _log(f"Resolved owned stream {_owned_stream_id} from key")
            return _owned_stream_id
    _log("No liveStream matches YOUTUBE_STREAM_KEY")
    return None


def list_broadcasts():
    out = []
    for status in ("active", "upcoming"):
        r = api(f"{API}/liveBroadcasts?part=snippet,status,contentDetails"
                f"&broadcastStatus={status}&maxResults=10")
        if r:
            out.extend(r.get("items", []))
    return out


def stream_status(stream_id):
    r = api(f"{API}/liveStreams?part=status&id={stream_id}")
    items = (r or {}).get("items", [])
    if not items:
        return ("", "")
    st = items[0].get("status", {})
    return (st.get("streamStatus", ""), st.get("healthStatus", {}).get("status", ""))


def ensure_broadcast(stream_id):
    body = {
        "snippet": {
            "title": BROADCAST_TITLE,
            "description": BROADCAST_DESCRIPTION,
            "scheduledStartTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "status": {"privacyStatus": BROADCAST_PRIVACY, "selfDeclaredMadeForKids": False},
        "contentDetails": {
            "enableAutoStart": False, "enableAutoStop": False,
            "latencyPreference": "ultraLow", "enableDvr": False,
        },
    }
    result = api(f"{API}/liveBroadcasts?part=snippet,status,contentDetails",
                 method="POST", body=body)
    if not result:
        _log("ensure_broadcast: create failed")
        return None
    bid = result.get("id")
    bind = api(f"{API}/liveBroadcasts/bind?part=id,contentDetails&id={bid}&streamId={stream_id}",
               method="POST")
    if bind:
        _log(f"Created+bound broadcast {bid} -> {stream_id}")
    return bid


def go_live(broadcast_id):
    return _transition(broadcast_id, "live")


def end_broadcast(broadcast_id):
    ok = _transition(broadcast_id, "complete")
    _set_privacy(broadcast_id, "private")
    return ok


def _transition(broadcast_id, target):
    r = api(f"{API}/liveBroadcasts/transition?part=id,status&id={broadcast_id}"
            f"&broadcastStatus={target}", method="POST")
    if r:
        _log(f"Transitioned broadcast {broadcast_id} -> {target}")
        return True
    return False


def _set_privacy(broadcast_id, privacy):
    cur = api(f"{API}/liveBroadcasts?part=snippet,status&id={broadcast_id}")
    if not cur or not cur.get("items"):
        return False
    snip = cur["items"][0].get("snippet", {})
    body = {"id": broadcast_id,
            "snippet": {"title": snip.get("title", ""),
                        "scheduledStartTime": snip.get("scheduledStartTime", "1970-01-01T00:00:00Z")},
            "status": {"privacyStatus": privacy}}
    return api(f"{API}/liveBroadcasts?part=snippet,status", method="PUT", body=body) is not None


def delete_broadcast(broadcast_id):
    token = get_access_token()
    if not token:
        return False
    try:
        req = urllib.request.Request(f"{API}/liveBroadcasts?id={broadcast_id}",
                                     method="DELETE", headers={"Authorization": f"Bearer {token}"})
        urllib.request.urlopen(req, timeout=15)
        _log(f"Deleted broadcast {broadcast_id}")
        return True
    except Exception as e:
        _log(f"delete_broadcast {broadcast_id} failed: {e}")
        return False
