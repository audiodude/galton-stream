# New vxstory Monitor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the galton monitor with a focused, modular monitor that observes the Hetzner box, owns the YouTube broadcast lifecycle *scoped strictly to its own stream*, drives the radio redirect, rides out drops, and alerts.

**Architecture:** Six flat modules under `monitor/` (no package; the Dockerfile copies `monitor/` to `/app` and runs `monitor.py`). The reconcile decision is a **pure planner** (`reconcile.plan_actions`) that returns a list of action tuples; `monitor.py` gathers state (I/O), calls the planner, and executes the actions (I/O). This makes the ownership-scoping guarantee unit-testable without touching the network — the headline regression guard.

**Tech Stack:** Python 3.11, stdlib (`urllib`, `datetime`, `zoneinfo`), `boto3` (redirect only, lazily imported). Tests: `pytest`, run via `uv run --with pytest python -m pytest`.

## Global Constraints

- **Ownership invariant (the bug fix):** the monitor only ever creates, binds, transitions, completes, privatizes, or deletes broadcasts whose `contentDetails.boundStreamId == get_owned_stream_id()` — the liveStream whose `cdn.ingestionInfo.streamName == YOUTUBE_STREAM_KEY`. A foreign broadcast is always a no-op.
- **`enableAutoStop=False`** on every created broadcast (restart resilience). **`enableAutoStart=False`** (monitor-driven go-live). **`enableDvr=False`**, **`latencyPreference="ultraLow"`**.
- **Redirects are 302**, never 301.
- Windows (America/Los_Angeles): operational `11:45–18:05`, consumer `12:00–18:00`. `FORCE_ACTIVE=1` overrides both to always-on.
- Ride-it-out: never tear down a broadcast inside the operational window; only `end_broadcast` outside it. Drops flip the redirect to the offline card and page; recovery restores and pages.
- Dropped from galton: `chat_poller`, `title_writer`, fallback ffmpeg, all Railway-restart/recovery escalation, `poll_health` against `galton-stream`.
- Modules are flat in `monitor/` and import each other as top-level modules (`import windows`, `from youtube import ...`). Tests live in `monitor/tests/` and prepend the parent dir to `sys.path` (mirrors `scripts/bake/tests/`).
- Reference implementation: `monitor/monitor.py` at tag `galton-monitor-orig` — port the marked functions faithfully; do not invent new behavior beyond this plan.

---

## File Structure

| File | Responsibility |
|---|---|
| `monitor/windows.py` | Pure window math (operational/consumer/next-boundary). |
| `monitor/youtube.py` | OAuth + API transport; pure helpers (`owned_broadcasts`, `is_recent`); lifecycle ops (`get_owned_stream_id`, `list_broadcasts`, `stream_status`, `ensure_broadcast`, `go_live`, `end_broadcast`, `delete_broadcast`). |
| `monitor/reconcile.py` | **Pure** `plan_actions(...) -> list[tuple]` — the ownership-scoped decision brain. |
| `monitor/redirect.py` | S3/CloudFront radio redirect (302), boto3 lazily imported, clients injectable. |
| `monitor/boxhealth.py` | Probe the box `/health` with a shared-secret bearer token. |
| `monitor/alerts.py` | Telegram + edge-triggered incident paging. |
| `monitor/monitor.py` | The loop: gather state → `plan_actions` → execute → alert → sleep. |
| `monitor/tests/test_*.py` | One test module per source module. |
| `monitor/Dockerfile` | Slimmed (drop ffmpeg + backup.png; keep boto3). |

---

### Task 1: `windows.py` — pure window math

**Files:**
- Create: `monitor/windows.py`
- Test: `monitor/tests/test_windows.py`

**Interfaces:**
- Produces: `in_operational_window(now=None) -> bool`, `in_consumer_window(now=None) -> bool`, `seconds_until_next_boundary(now=None) -> float`, constants `ACTIVE_TZ`, `OPERATIONAL_START/END`, `CONSUMER_START/END`. `now` is a tz-aware `datetime` (defaults to current PT). `FORCE_ACTIVE=1` env forces both windows True.

- [ ] **Step 1: Write the failing test**

```python
# monitor/tests/test_windows.py
import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import windows
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
def at(h, m): return datetime.datetime(2026, 6, 19, h, m, tzinfo=PT)

def test_operational_window_bounds():
    assert not windows.in_operational_window(at(11, 44))
    assert windows.in_operational_window(at(11, 45))      # inclusive start
    assert windows.in_operational_window(at(18, 4))
    assert not windows.in_operational_window(at(18, 5))   # exclusive end

def test_consumer_window_bounds():
    assert not windows.in_consumer_window(at(11, 59))
    assert windows.in_consumer_window(at(12, 0))
    assert not windows.in_consumer_window(at(18, 0))

def test_operational_brackets_consumer():
    # 11:45-12:00 warmup and 18:00-18:05 cooldown are operational but not consumer
    assert windows.in_operational_window(at(11, 50)) and not windows.in_consumer_window(at(11, 50))
    assert windows.in_operational_window(at(18, 2)) and not windows.in_consumer_window(at(18, 2))

def test_seconds_until_next_boundary_positive_and_bounded():
    s = windows.seconds_until_next_boundary(at(13, 0))
    assert 0 < s <= 24 * 3600
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest monitor/tests/test_windows.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'windows'`.

- [ ] **Step 3: Write minimal implementation** (port from `galton-monitor-orig` lines 84–144)

```python
# monitor/windows.py
"""Operational/consumer window math (America/Los_Angeles). Pure: pass `now`."""
import datetime
import os
from zoneinfo import ZoneInfo

ACTIVE_TZ = ZoneInfo("America/Los_Angeles")
OPERATIONAL_START = (11, 45)
CONSUMER_START = (12, 0)
CONSUMER_END = (18, 0)
OPERATIONAL_END = (18, 5)

FORCE_ACTIVE = os.environ.get("FORCE_ACTIVE", "") == "1"


def _in_window(start_hm, end_hm, now=None):
    now = now or datetime.datetime.now(ACTIVE_TZ)
    hm = (now.hour, now.minute)
    return start_hm <= hm < end_hm


def in_operational_window(now=None):
    return FORCE_ACTIVE or _in_window(OPERATIONAL_START, OPERATIONAL_END, now)


def in_consumer_window(now=None):
    return FORCE_ACTIVE or _in_window(CONSUMER_START, CONSUMER_END, now)


def seconds_until_next_boundary(now=None):
    """Seconds to the next of the four window edges, so the loop wakes within a
    second of 12:00 / 18:00 / etc."""
    now = now or datetime.datetime.now(ACTIVE_TZ)
    today = now.date()
    tomorrow = today + datetime.timedelta(days=1)
    candidates = []
    for h, m in (OPERATIONAL_START, CONSUMER_START, CONSUMER_END, OPERATIONAL_END):
        for day in (today, tomorrow):
            t = datetime.datetime.combine(day, datetime.time(h, m), tzinfo=ACTIVE_TZ)
            if t > now:
                candidates.append((t - now).total_seconds())
    return min(candidates) if candidates else float("inf")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest python -m pytest monitor/tests/test_windows.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add monitor/windows.py monitor/tests/test_windows.py
git commit -m "feat(monitor): windows.py — pure operational/consumer window math"
```

---

### Task 2: `youtube.py` — OAuth transport + pure ownership helpers

**Files:**
- Create: `monitor/youtube.py`
- Test: `monitor/tests/test_youtube_core.py`

**Interfaces:**
- Produces: `get_access_token() -> str|None`, `api(url, method="GET", body=None) -> dict|None` (authenticated JSON request; tests monkeypatch this), and pure helpers:
  - `owned_broadcasts(broadcasts: list[dict], stream_id: str) -> list[dict]` — filter to `contentDetails.boundStreamId == stream_id`.
  - `life(b: dict) -> str` — `b["status"]["lifeCycleStatus"]` or `""`.
  - `is_recent(b: dict, now: datetime, max_age_min=15) -> bool` — scheduledStartTime within N minutes of `now`.
- Consumes: nothing (Task 1 independent).

- [ ] **Step 1: Write the failing test**

```python
# monitor/tests/test_youtube_core.py
import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import youtube
from zoneinfo import ZoneInfo

UTC = datetime.timezone.utc

def _b(bid, life, bound, sst=None):
    return {"id": bid,
            "status": {"lifeCycleStatus": life},
            "contentDetails": {"boundStreamId": bound},
            "snippet": {"scheduledStartTime": sst} if sst else {}}

def test_owned_broadcasts_filters_by_bound_stream():
    bs = [_b("mine", "live", "S1"), _b("theirs", "live", "S2"), _b("mine2", "ready", "S1")]
    owned = youtube.owned_broadcasts(bs, "S1")
    assert [b["id"] for b in owned] == ["mine", "mine2"]

def test_owned_broadcasts_excludes_unbound():
    bs = [{"id": "x", "status": {"lifeCycleStatus": "live"}, "contentDetails": {}}]
    assert youtube.owned_broadcasts(bs, "S1") == []

def test_life_default_empty():
    assert youtube.life({"status": {}}) == ""
    assert youtube.life(_b("a", "testing", "S1")) == "testing"

def test_is_recent():
    now = datetime.datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    fresh = _b("a", "ready", "S1", sst="2026-06-19T11:55:00Z")   # 5 min ago
    stale = _b("b", "ready", "S1", sst="2026-06-19T11:30:00Z")   # 30 min ago
    assert youtube.is_recent(fresh, now)
    assert not youtube.is_recent(stale, now)
    assert not youtube.is_recent(_b("c", "ready", "S1"), now)    # no sst

def test_get_access_token_caches(monkeypatch):
    calls = {"n": 0}
    class FakeResp:
        def read(self): return b'{"access_token": "tok", "expires_in": 3600}'
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=0):
        calls["n"] += 1
        return FakeResp()
    monkeypatch.setattr(youtube.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(youtube, "YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setattr(youtube, "YOUTUBE_REFRESH_TOKEN", "rt")
    youtube._access_token = None
    youtube._token_expires = 0
    assert youtube.get_access_token() == "tok"
    assert youtube.get_access_token() == "tok"   # cached
    assert calls["n"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest monitor/tests/test_youtube_core.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'youtube'`.

- [ ] **Step 3: Write minimal implementation** (OAuth + `api` ported from `galton-monitor-orig` 179–252; helpers new + ported `_broadcast_is_recent` 526–537)

```python
# monitor/youtube.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest python -m pytest monitor/tests/test_youtube_core.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add monitor/youtube.py monitor/tests/test_youtube_core.py
git commit -m "feat(monitor): youtube.py OAuth transport + ownership helpers"
```

---

### Task 3: `youtube.py` — lifecycle ops (stream resolution, ensure/go-live/end)

**Files:**
- Modify: `monitor/youtube.py` (append functions)
- Test: `monitor/tests/test_youtube_ops.py`

**Interfaces:**
- Consumes: `api` (Task 2, monkeypatched in tests), `BROADCAST_*` constants.
- Produces:
  - `get_owned_stream_id() -> str|None` — resolve & cache the liveStream id by `streamName == YOUTUBE_STREAM_KEY`.
  - `list_broadcasts() -> list[dict]` — active + upcoming, with `snippet,status,contentDetails`.
  - `stream_status(stream_id) -> tuple[str, str]` — `(streamStatus, healthStatus)`, e.g. `("active","good")`; `("", "")` if unknown.
  - `ensure_broadcast(stream_id) -> str|None` — create (autostart False, autostop False, ultraLow, no DVR, config title/privacy) + bind; returns new id.
  - `go_live(broadcast_id) -> bool` — transition → `live`.
  - `end_broadcast(broadcast_id) -> bool` — transition → `complete`, then set privacy private.
  - `delete_broadcast(broadcast_id) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# monitor/tests/test_youtube_ops.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import youtube

class Recorder:
    def __init__(self, responses): self.responses, self.calls = responses, []
    def __call__(self, url, method="GET", body=None):
        self.calls.append((method, url, body))
        for frag, resp in self.responses.items():
            if frag in url: return resp
        return {}

def test_get_owned_stream_id_matches_key(monkeypatch):
    monkeypatch.setattr(youtube, "YOUTUBE_STREAM_KEY", "live2-abc")
    youtube._owned_stream_id = None
    rec = Recorder({"liveStreams": {"items": [
        {"id": "S_other", "cdn": {"ingestionInfo": {"streamName": "nope"}}},
        {"id": "S_mine", "cdn": {"ingestionInfo": {"streamName": "live2-abc"}}},
    ]}})
    monkeypatch.setattr(youtube, "api", rec)
    assert youtube.get_owned_stream_id() == "S_mine"

def test_stream_status(monkeypatch):
    rec = Recorder({"liveStreams": {"items": [
        {"id": "S1", "status": {"streamStatus": "active", "healthStatus": {"status": "good"}}}]}})
    monkeypatch.setattr(youtube, "api", rec)
    assert youtube.stream_status("S1") == ("active", "good")

def test_ensure_broadcast_sets_autostop_false_autostart_false(monkeypatch):
    rec = Recorder({"liveBroadcasts?part=snippet,status,contentDetails": {"id": "B1"},
                    "/bind": {"id": "B1"}})
    monkeypatch.setattr(youtube, "api", rec)
    bid = youtube.ensure_broadcast("S1")
    assert bid == "B1"
    create = next(c for c in rec.calls if c[0] == "POST" and "part=snippet,status,contentDetails" in c[1])
    cd = create[2]["contentDetails"]
    assert cd["enableAutoStart"] is False and cd["enableAutoStop"] is False
    assert cd["enableDvr"] is False and cd["latencyPreference"] == "ultraLow"
    assert any("/bind" in c[1] and "streamId=S1" in c[1] for c in rec.calls)

def test_go_live_transitions_to_live(monkeypatch):
    rec = Recorder({"/transition": {"id": "B1"}})
    monkeypatch.setattr(youtube, "api", rec)
    assert youtube.go_live("B1") is True
    assert any("broadcastStatus=live" in c[1] and "id=B1" in c[1] for c in rec.calls)

def test_end_broadcast_completes_then_private(monkeypatch):
    rec = Recorder({"/transition": {"id": "B1"},
                    "liveBroadcasts?part=snippet,status&id=B1": {"items": [
                        {"id": "B1", "snippet": {"title": "t", "scheduledStartTime": "1970-01-01T00:00:00Z"}}]},
                    "liveBroadcasts?part=snippet,status": {"id": "B1"}})
    monkeypatch.setattr(youtube, "api", rec)
    assert youtube.end_broadcast("B1") is True
    assert any("broadcastStatus=complete" in c[1] for c in rec.calls)
    assert any(c[0] == "PUT" and c[2] and c[2].get("status", {}).get("privacyStatus") == "private" for c in rec.calls)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest monitor/tests/test_youtube_ops.py -v`
Expected: FAIL with `AttributeError: module 'youtube' has no attribute 'get_owned_stream_id'`.

- [ ] **Step 3: Write minimal implementation** (append to `monitor/youtube.py`; ported from `galton-monitor-orig` 269–334, 340–416, 205–227)

```python
# --- lifecycle ops (append to monitor/youtube.py) ---

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest python -m pytest monitor/tests/test_youtube_ops.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add monitor/youtube.py monitor/tests/test_youtube_ops.py
git commit -m "feat(monitor): youtube.py lifecycle ops (owned-stream scoped)"
```

---

### Task 4: `reconcile.py` — pure action planner (headline regression guard)

**Files:**
- Create: `monitor/reconcile.py`
- Test: `monitor/tests/test_reconcile.py`

**Interfaces:**
- Consumes: `youtube.owned_broadcasts`, `youtube.life`, `youtube.is_recent` (Task 2).
- Produces: `plan_actions(now, in_op, in_consumer, stream_id, broadcasts, stream_active, current_redirect_vid) -> list[tuple]`. Action tuples: `("delete_broadcast", bid)`, `("create_broadcast",)`, `("go_live", bid)`, `("end_broadcast", bid)`, `("redirect_online", video_id)`, `("redirect_offline",)`.

- [ ] **Step 1: Write the failing test**

```python
# monitor/tests/test_reconcile.py
import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import reconcile
UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 6, 19, 13, 0, tzinfo=UTC)

def b(bid, life, bound, sst="2026-06-19T12:59:30Z"):  # recent by default
    return {"id": bid, "status": {"lifeCycleStatus": life},
            "contentDetails": {"boundStreamId": bound},
            "snippet": {"scheduledStartTime": sst}}

def plan(**kw):
    base = dict(now=NOW, in_op=True, in_consumer=True, stream_id="S1",
                broadcasts=[], stream_active=False, current_redirect_vid=None)
    base.update(kw)
    return reconcile.plan_actions(**base)

# THE headline regression: out of window, a FOREIGN live broadcast must be untouched
def test_out_of_window_foreign_live_is_noop():
    acts = plan(in_op=False, in_consumer=False,
                broadcasts=[b("foreign", "live", "S_OTHER")])
    assert ("end_broadcast", "foreign") not in acts
    assert all(a[0] != "end_broadcast" for a in acts)

def test_out_of_window_owned_live_is_ended():
    acts = plan(in_op=False, in_consumer=False,
                broadcasts=[b("mine", "live", "S1")])
    assert ("end_broadcast", "mine") in acts

def test_in_window_no_broadcast_creates():
    assert ("create_broadcast",) in plan(broadcasts=[])

def test_in_window_recent_pending_goes_live_when_stream_active():
    acts = plan(broadcasts=[b("mine", "ready", "S1")], stream_active=True)
    assert ("go_live", "mine") in acts

def test_in_window_pending_waits_when_stream_inactive():
    acts = plan(broadcasts=[b("mine", "ready", "S1")], stream_active=False)
    assert all(a[0] not in ("go_live", "create_broadcast") for a in acts)

def test_redirect_online_only_in_consumer_with_live_active():
    acts = plan(broadcasts=[b("mine", "live", "S1")], stream_active=True)
    assert ("redirect_online", "mine") in acts

def test_redirect_offline_when_outside_consumer():
    acts = plan(in_op=True, in_consumer=False, broadcasts=[b("mine", "live", "S1")],
                stream_active=True, current_redirect_vid="mine")
    assert ("redirect_offline",) in acts

def test_redirect_online_not_repeated_when_already_pointed():
    acts = plan(broadcasts=[b("mine", "live", "S1")], stream_active=True,
                current_redirect_vid="mine")
    assert all(a[0] != "redirect_online" for a in acts)

def test_in_window_stale_pending_deleted():
    acts = plan(broadcasts=[b("old", "ready", "S1", sst="2026-06-19T12:00:00Z")])  # 60 min old
    assert ("delete_broadcast", "old") in acts
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest monitor/tests/test_reconcile.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'reconcile'`.

- [ ] **Step 3: Write minimal implementation**

```python
# monitor/reconcile.py
"""Pure reconcile planner. Given current state, return the list of actions to
take. No I/O — every action is a tuple the executor in monitor.py runs. All
broadcast actions are scoped to the owned stream via youtube.owned_broadcasts."""
import youtube


def plan_actions(now, in_op, in_consumer, stream_id, broadcasts, stream_active,
                 current_redirect_vid):
    owned = youtube.owned_broadcasts(broadcasts, stream_id)
    live = next((b for b in owned if youtube.life(b) == "live"), None)
    pending = [b for b in owned if youtube.life(b) in ("created", "ready", "testing")]
    recent_pending = next((b for b in pending if youtube.is_recent(b, now)), None)
    actions = []

    if not in_op:
        for b in owned:
            if youtube.life(b) in ("live", "testing"):
                actions.append(("end_broadcast", b["id"]))
        if current_redirect_vid is not None:
            actions.append(("redirect_offline",))
        return actions

    # in operational window
    for b in pending:
        if not youtube.is_recent(b, now):
            actions.append(("delete_broadcast", b["id"]))

    if live is None and recent_pending is None:
        actions.append(("create_broadcast",))
    elif live is None and recent_pending is not None and stream_active:
        actions.append(("go_live", recent_pending["id"]))

    target = live["id"] if (in_consumer and live is not None and stream_active) else None
    if target is not None and current_redirect_vid != target:
        actions.append(("redirect_online", target))
    elif target is None and current_redirect_vid is not None:
        actions.append(("redirect_offline",))

    return actions
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest python -m pytest monitor/tests/test_reconcile.py -v`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add monitor/reconcile.py monitor/tests/test_reconcile.py
git commit -m "feat(monitor): reconcile.py pure planner + ownership-scoping tests"
```

---

### Task 5: `redirect.py` — radio redirect (302), injectable clients

**Files:**
- Create: `monitor/redirect.py`
- Test: `monitor/tests/test_redirect.py`

**Interfaces:**
- Produces: `current_video_id(s3=None) -> str|None`, `set_radio_online(video_id, s3=None, cf=None) -> bool`, `set_radio_offline(s3=None, cf=None) -> bool`. Clients default to lazily-created boto3 clients; tests pass mocks so `boto3` is never imported.

- [ ] **Step 1: Write the failing test**

```python
# monitor/tests/test_redirect.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import redirect

class FakeS3:
    def __init__(self, rules=None): self.cfg = {"RoutingRules": rules} if rules else {}; self.put = None
    def get_bucket_website(self, Bucket): return self.cfg
    def put_bucket_website(self, Bucket, WebsiteConfiguration): self.put = WebsiteConfiguration
    def put_object(self, **kw): pass
class FakeCF:
    def create_invalidation(self, **kw): self.inv = kw

def test_set_radio_online_writes_302_live_rule():
    s3, cf = FakeS3(), FakeCF()
    assert redirect.set_radio_online("VID123", s3=s3, cf=cf) is True
    rule = s3.put["RoutingRules"][0]["Redirect"]
    assert rule["HttpRedirectCode"] == "302"
    assert rule["ReplaceKeyWith"] == "live/VID123"
    assert rule["HostName"] == "www.youtube.com"

def test_set_radio_offline_drops_rule():
    s3, cf = FakeS3(rules=[{"Redirect": {"ReplaceKeyWith": "live/X"}}]), FakeCF()
    assert redirect.set_radio_offline(s3=s3, cf=cf) is True
    assert "RoutingRules" not in s3.put

def test_current_video_id_reads_rule():
    s3 = FakeS3(rules=[{"Redirect": {"ReplaceKeyWith": "live/ABC"}}])
    assert redirect.current_video_id(s3=s3) == "ABC"
    assert redirect.current_video_id(s3=FakeS3()) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest monitor/tests/test_redirect.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'redirect'`.

- [ ] **Step 3: Write minimal implementation** (ported from `galton-monitor-orig` 436–523; boto3 lazy)

```python
# monitor/redirect.py
"""radio.dangerthirdrail.com S3 website redirect + CloudFront invalidation.
In-window: 302 -> youtube.com/live/<id>. Out: drop the rule (serve index.html)."""
import os
import sys
import time

RADIO_BUCKET = os.environ.get("RADIO_BUCKET", "radio.dangerthirdrail.com")
RADIO_CF_DISTRIBUTION_ID = os.environ.get("RADIO_CF_DISTRIBUTION_ID", "E24RTA588S2VSH")
RADIO_REGION = os.environ.get("RADIO_REGION", "us-east-1")
RADIO_OFFLINE_HTML_PATH = os.environ.get("RADIO_OFFLINE_HTML_PATH", "/app/radio-offline.html")

_s3 = _cf = None


def _log(msg):
    print(f"[monitor] {msg}", file=sys.stderr, flush=True)


def _clients(s3, cf):
    global _s3, _cf
    if s3 is None or cf is None:
        import boto3
        if _s3 is None:
            _s3 = boto3.client("s3", region_name=RADIO_REGION)
            _cf = boto3.client("cloudfront", region_name=RADIO_REGION)
    return (s3 or _s3), (cf or _cf)


def _invalidate(cf):
    try:
        cf.create_invalidation(
            DistributionId=RADIO_CF_DISTRIBUTION_ID,
            InvalidationBatch={"Paths": {"Quantity": 1, "Items": ["/*"]},
                               "CallerReference": f"radio-{int(time.time())}"})
    except Exception as e:
        _log(f"CloudFront invalidation failed: {e}")


def current_video_id(s3=None):
    s3, _ = _clients(s3, s3)
    try:
        cfg = s3.get_bucket_website(Bucket=RADIO_BUCKET)
    except Exception as e:
        _log(f"get_bucket_website failed: {e}")
        return None
    rules = cfg.get("RoutingRules") or []
    if not rules:
        return None
    key = rules[0].get("Redirect", {}).get("ReplaceKeyWith", "")
    return key[len("live/"):] if key.startswith("live/") else None


def set_radio_online(video_id, s3=None, cf=None):
    s3, cf = _clients(s3, cf)
    try:
        s3.put_bucket_website(Bucket=RADIO_BUCKET, WebsiteConfiguration={
            "IndexDocument": {"Suffix": "index.html"},
            "RoutingRules": [{"Redirect": {
                "HostName": "www.youtube.com", "HttpRedirectCode": "302",
                "Protocol": "https", "ReplaceKeyWith": f"live/{video_id}"}}]})
        _invalidate(cf)
        _log(f"Radio ONLINE -> youtube.com/live/{video_id}")
        return True
    except Exception as e:
        _log(f"set_radio_online failed: {e}")
        return False


def set_radio_offline(s3=None, cf=None):
    s3, cf = _clients(s3, cf)
    try:
        if os.path.exists(RADIO_OFFLINE_HTML_PATH):
            with open(RADIO_OFFLINE_HTML_PATH, "rb") as f:
                s3.put_object(Bucket=RADIO_BUCKET, Key="index.html", Body=f.read(),
                              ContentType="text/html; charset=utf-8",
                              CacheControl="public, max-age=60")
        s3.put_bucket_website(Bucket=RADIO_BUCKET,
                              WebsiteConfiguration={"IndexDocument": {"Suffix": "index.html"}})
        _invalidate(cf)
        _log("Radio OFFLINE -> serving index.html")
        return True
    except Exception as e:
        _log(f"set_radio_offline failed: {e}")
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest python -m pytest monitor/tests/test_redirect.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add monitor/redirect.py monitor/tests/test_redirect.py
git commit -m "feat(monitor): redirect.py — 302 radio redirect, injectable clients"
```

---

### Task 6: `boxhealth.py` — probe the box `/health`

**Files:**
- Create: `monitor/boxhealth.py`
- Test: `monitor/tests/test_boxhealth.py`

**Interfaces:**
- Produces: `probe() -> dict|None` — GET `BOX_HEALTH_URL` with `Authorization: Bearer BOX_HEALTH_TOKEN`, 8 s timeout; parsed JSON or `None`.

- [ ] **Step 1: Write the failing test**

```python
# monitor/tests/test_boxhealth.py
import os, sys, io, urllib.error
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import boxhealth

class FakeResp:
    def __init__(self, payload): self._p = payload
    def read(self): return self._p
    def __enter__(self): return self
    def __exit__(self, *a): return False

def test_probe_returns_json(monkeypatch):
    monkeypatch.setattr(boxhealth, "BOX_HEALTH_URL", "https://box/health")
    monkeypatch.setattr(boxhealth, "BOX_HEALTH_TOKEN", "tok")
    captured = {}
    def fake_urlopen(req, timeout=0):
        captured["auth"] = req.headers.get("Authorization")
        return FakeResp(b'{"playout_alive": true, "ffmpeg_alive": true, "heartbeat_age_s": 2}')
    monkeypatch.setattr(boxhealth.urllib.request, "urlopen", fake_urlopen)
    out = boxhealth.probe()
    assert out["ffmpeg_alive"] is True and out["heartbeat_age_s"] == 2
    assert captured["auth"] == "Bearer tok"

def test_probe_unreachable_returns_none(monkeypatch):
    monkeypatch.setattr(boxhealth, "BOX_HEALTH_URL", "https://box/health")
    def boom(req, timeout=0): raise urllib.error.URLError("nope")
    monkeypatch.setattr(boxhealth.urllib.request, "urlopen", boom)
    assert boxhealth.probe() is None

def test_probe_no_url_returns_none(monkeypatch):
    monkeypatch.setattr(boxhealth, "BOX_HEALTH_URL", "")
    assert boxhealth.probe() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest monitor/tests/test_boxhealth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'boxhealth'`.

- [ ] **Step 3: Write minimal implementation**

```python
# monitor/boxhealth.py
"""Probe the Hetzner box /health endpoint (diagnostic enrichment for alerts;
never gates a lifecycle decision). Bearer-token auth since it's public-facing."""
import json
import os
import sys
import urllib.error
import urllib.request

BOX_HEALTH_URL = os.environ.get("BOX_HEALTH_URL", "")
BOX_HEALTH_TOKEN = os.environ.get("BOX_HEALTH_TOKEN", "")


def _log(msg):
    print(f"[monitor] {msg}", file=sys.stderr, flush=True)


def probe():
    if not BOX_HEALTH_URL:
        return None
    headers = {}
    if BOX_HEALTH_TOKEN:
        headers["Authorization"] = f"Bearer {BOX_HEALTH_TOKEN}"
    try:
        req = urllib.request.Request(BOX_HEALTH_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        _log(f"box health probe failed: {e}")
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest python -m pytest monitor/tests/test_boxhealth.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add monitor/boxhealth.py monitor/tests/test_boxhealth.py
git commit -m "feat(monitor): boxhealth.py — token-authed box /health probe"
```

---

### Task 7: `alerts.py` — Telegram + edge-triggered paging

**Files:**
- Create: `monitor/alerts.py`
- Test: `monitor/tests/test_alerts.py`

**Interfaces:**
- Produces: `class Alerter` with `update(state, reason) -> None`. States: `"OFF"`, `"WAITING"`, `"LIVE"`, `"DEGRADED"`. Pages (Telegram) only on edges INTO `"DEGRADED"` (problem) and the first `"LIVE"`/`"OFF"` after a `"DEGRADED"` incident (recovery). Routine transitions (OFF↔WAITING↔LIVE without a prior incident) are logged, never paged. `send(text)` sends the Telegram message (no-op log if creds unset).

- [ ] **Step 1: Write the failing test**

```python
# monitor/tests/test_alerts.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import alerts

def make(monkeypatch):
    sent = []
    a = alerts.Alerter()
    monkeypatch.setattr(a, "send", lambda text: sent.append(text))
    return a, sent

def test_routine_transitions_do_not_page(monkeypatch):
    a, sent = make(monkeypatch)
    for st in ["OFF", "WAITING", "LIVE", "OFF"]:
        a.update(st, "routine")
    assert sent == []

def test_degraded_pages_once_and_recovery_pages(monkeypatch):
    a, sent = make(monkeypatch)
    a.update("LIVE", "ok")
    a.update("DEGRADED", "stream dropped")
    a.update("DEGRADED", "still down")   # no second page while still degraded
    a.update("LIVE", "recovered")
    assert len(sent) == 2
    assert "stream dropped" in sent[0] and "recovered" in sent[1]

def test_off_after_incident_counts_as_recovery(monkeypatch):
    a, sent = make(monkeypatch)
    a.update("LIVE", "ok"); a.update("DEGRADED", "drop"); a.update("OFF", "window close")
    assert len(sent) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest monitor/tests/test_alerts.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'alerts'`.

- [ ] **Step 3: Write minimal implementation** (Telegram ported from `galton-monitor-orig` 159–176; edge discipline adapted from 651–678)

```python
# monitor/alerts.py
"""Telegram alerting with edge-triggered incident discipline: page when we drop
into DEGRADED, and again when the incident clears. Routine churn stays quiet."""
import os
import sys
import urllib.parse
import urllib.request

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PREFIX = "vxstory monitor:"


def _log(msg):
    print(f"[monitor] {msg}", file=sys.stderr, flush=True)


class Alerter:
    def __init__(self):
        self.state = "OFF"
        self.incident = False

    def send(self, text):
        if not BOT_TOKEN or not CHAT_ID:
            _log(f"(no telegram) {text}")
            return
        try:
            data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=data)
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            _log(f"Telegram send failed: {e}")

    def update(self, state, reason):
        if state == self.state:
            return
        msg = f"{PREFIX} {self.state} -> {state}. {reason}"
        _log(msg)
        if state == "DEGRADED":
            if not self.incident:
                self.incident = True
                self.send(msg)
        elif self.incident:           # any move out of DEGRADED clears the incident
            self.incident = False
            self.send(msg)
        self.state = state
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest python -m pytest monitor/tests/test_alerts.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add monitor/alerts.py monitor/tests/test_alerts.py
git commit -m "feat(monitor): alerts.py — edge-triggered Telegram paging"
```

---

### Task 8: `monitor.py` — reconcile loop (executor + state) replacing the monolith

**Files:**
- Modify (replace contents): `monitor/monitor.py`
- Test: `monitor/tests/test_executor.py`

**Interfaces:**
- Consumes: all modules above.
- Produces: `execute(action) -> None` (maps an action tuple to youtube/redirect calls), `classify(in_op, in_consumer, live, stream_active, degraded_polls) -> str` (state label for the Alerter), `main()` (the loop).

- [ ] **Step 1: Write the failing test**

```python
# monitor/tests/test_executor.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor

def test_execute_maps_actions(monkeypatch):
    calls = []
    monkeypatch.setattr(monitor.youtube, "ensure_broadcast", lambda sid: calls.append(("create", sid)))
    monkeypatch.setattr(monitor.youtube, "go_live", lambda bid: calls.append(("go_live", bid)))
    monkeypatch.setattr(monitor.youtube, "end_broadcast", lambda bid: calls.append(("end", bid)))
    monkeypatch.setattr(monitor.youtube, "delete_broadcast", lambda bid: calls.append(("del", bid)))
    monkeypatch.setattr(monitor.redirect, "set_radio_online", lambda vid: calls.append(("on", vid)))
    monkeypatch.setattr(monitor.redirect, "set_radio_offline", lambda: calls.append(("off",)))
    monitor.STREAM_ID = "S1"
    monitor.execute(("create_broadcast",))
    monitor.execute(("go_live", "B1"))
    monitor.execute(("end_broadcast", "B1"))
    monitor.execute(("delete_broadcast", "B2"))
    monitor.execute(("redirect_online", "VID"))
    monitor.execute(("redirect_offline",))
    assert calls == [("create", "S1"), ("go_live", "B1"), ("end", "B1"),
                     ("del", "B2"), ("on", "VID"), ("off",)]

def test_classify_states():
    assert monitor.classify(False, False, None, False, 0) == "OFF"
    assert monitor.classify(True, True, {"id": "B"}, True, 0) == "LIVE"
    assert monitor.classify(True, True, None, False, 0) == "WAITING"
    assert monitor.classify(True, True, None, False, 5) == "DEGRADED"   # past grace
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest monitor/tests/test_executor.py -v`
Expected: FAIL — current `monitor.py` has no `execute`/`classify` and imports galton-only symbols.

- [ ] **Step 3: Write minimal implementation** (replace the entire file)

```python
#!/usr/bin/env python3
"""vxstory monitor: observe the Hetzner box, own the YouTube broadcast lifecycle
(scoped to our stream), drive the radio redirect, ride out drops, alert.

Loop: gather state (windows + broadcasts + stream status + box health) -> pure
reconcile.plan_actions -> execute actions -> update alerter -> sleep to the next
poll tick or window edge."""
import datetime
import os
import time

import alerts
import boxhealth
import reconcile
import redirect
import windows
import youtube

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "120"))
DEGRADED_GRACE_POLLS = int(os.environ.get("DEGRADED_GRACE_POLLS", "3"))
UTC = datetime.timezone.utc

STREAM_ID = None  # resolved once at first successful poll


def _log(msg):
    import sys
    print(f"[monitor] {msg}", file=sys.stderr, flush=True)


def execute(action):
    kind = action[0]
    if kind == "create_broadcast":
        youtube.ensure_broadcast(STREAM_ID)
    elif kind == "go_live":
        youtube.go_live(action[1])
    elif kind == "end_broadcast":
        youtube.end_broadcast(action[1])
    elif kind == "delete_broadcast":
        youtube.delete_broadcast(action[1])
    elif kind == "redirect_online":
        redirect.set_radio_online(action[1])
    elif kind == "redirect_offline":
        redirect.set_radio_offline()


def classify(in_op, in_consumer, live, stream_active, degraded_polls):
    if not in_op:
        return "OFF"
    if live is not None and stream_active:
        return "LIVE"
    if degraded_polls >= DEGRADED_GRACE_POLLS:
        return "DEGRADED"
    return "WAITING"


def main():
    global STREAM_ID
    _log(f"vxstory monitor starting, poll every {POLL_INTERVAL}s")
    alerter = alerts.Alerter()
    degraded_polls = 0
    first = True
    while True:
        if not first:
            time.sleep(max(1, min(POLL_INTERVAL, windows.seconds_until_next_boundary() + 1)))
        first = False

        now = datetime.datetime.now(UTC)
        in_op = windows.in_operational_window()
        in_consumer = windows.in_consumer_window()

        STREAM_ID = youtube.get_owned_stream_id()
        if STREAM_ID is None:
            _log("cannot resolve owned stream; skipping poll")
            alerter.update("DEGRADED" if in_op else "OFF", "owned stream unresolved")
            continue

        broadcasts = youtube.list_broadcasts()
        owned = youtube.owned_broadcasts(broadcasts, STREAM_ID)
        live = next((b for b in owned if youtube.life(b) == "live"), None)
        ss, _health = youtube.stream_status(STREAM_ID)
        stream_active = ss == "active"
        cur_vid = redirect.current_video_id()

        actions = reconcile.plan_actions(now, in_op, in_consumer, STREAM_ID,
                                         broadcasts, stream_active, cur_vid)
        for a in actions:
            try:
                execute(a)
            except Exception as e:
                _log(f"action {a} failed: {e}")

        # alert state
        if in_op and live is None and not stream_active:
            degraded_polls += 1
        else:
            degraded_polls = 0
        state = classify(in_op, in_consumer, live, stream_active, degraded_polls)
        reason = f"stream={ss or 'none'}, live={'yes' if live else 'no'}"
        if state == "DEGRADED":
            box = boxhealth.probe()
            reason += "; box=" + ("unreachable" if box is None else "alive")
        alerter.update(state, reason)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest python -m pytest monitor/tests/test_executor.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the full suite**

Run: `uv run --with pytest python -m pytest monitor/tests/ -v`
Expected: PASS (all modules green).

- [ ] **Step 6: Commit**

```bash
git add monitor/monitor.py monitor/tests/test_executor.py
git commit -m "feat(monitor): rewrite monitor.py as gather->plan->execute loop"
```

---

### Task 9: Slim the Dockerfile + document env

**Files:**
- Modify: `monitor/Dockerfile`
- Create: `monitor/README.md`

**Interfaces:** none (deploy config + docs).

- [ ] **Step 1: Rewrite `monitor/Dockerfile`** (drop ffmpeg + backup.png; keep boto3; still copies all modules)

```dockerfile
FROM python:3.11-slim

RUN pip3 install --no-cache-dir boto3

WORKDIR /app

COPY monitor/ /app/
COPY assets/radio-offline.html /app/radio-offline.html

CMD ["python3", "monitor.py"]
```

- [ ] **Step 2: Verify the image builds**

Run: `docker build -f monitor/Dockerfile -t vxstory-monitor-test .`
Expected: build succeeds (`naming to docker.io/library/vxstory-monitor-test`).

- [ ] **Step 3: Write `monitor/README.md`** documenting required env

```markdown
# vxstory monitor

Observes the Hetzner playout box, owns the YouTube broadcast lifecycle (scoped
to its own stream), drives the radio.dangerthirdrail.com redirect, alerts.

## Required env (Railway service `galton-monitor` / `vxstory-monitor`)
- `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`
- `YOUTUBE_STREAM_KEY` — the owned production stream key (the box pushes to this).
- `BROADCAST_TITLE`, `BROADCAST_DESCRIPTION`, `BROADCAST_PRIVACY` (default `public`)
- `BOX_HEALTH_URL`, `BOX_HEALTH_TOKEN` — box `/health` + shared secret.
- `RADIO_BUCKET`, `RADIO_CF_DISTRIBUTION_ID`, `RADIO_REGION`, `RADIO_OFFLINE_HTML_PATH`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- Optional: `POLL_INTERVAL` (120), `DEGRADED_GRACE_POLLS` (3), `FORCE_ACTIVE` (`1` = always-on)

## Removed vs galton-monitor-orig
`GALTON_STREAM_URL`, `RAILWAY_API_TOKEN`, `GALTON_STREAM_SERVICE_ID`, and all
chat/title/fallback/Railway-restart logic. Window definition must match the box's
playout scheduling.
```

- [ ] **Step 4: Commit**

```bash
git add monitor/Dockerfile monitor/README.md
git commit -m "chore(monitor): slim Dockerfile, document env for vxstory monitor"
```

---

## Self-Review

**Spec coverage:**
- Ownership invariant → Tasks 2 (`owned_broadcasts`) + 4 (planner scoping tests, incl. the foreign-live no-op). ✅
- `enableAutoStop=False`/`autoStart=False`/`ultraLow`/`no DVR` → Task 3 + test. ✅
- 302 redirect → Task 5 + test. ✅
- windows (operational/consumer) → Task 1. ✅
- monitor-driven go-live → Tasks 3/4/8. ✅
- ride-it-out (no in-window teardown; redirect offline + page on drop; recover) → Task 4 (no end inside window) + Task 8 (DEGRADED state + Alerter). ✅
- box `/health` token probe → Task 6. ✅
- drop chat/title/Railway-restart → Task 8 (rewrite) + Task 9 (Dockerfile/env). ✅
- testing emphasis (scoping regression; table-driven reconcile) → Task 4. ✅
- box-side contract (window-gated playout, `/health`) is a **separate plan** per the spec — not implemented here; Task 9 README notes the dependency.

**Placeholder scan:** none — every code step is complete.

**Type consistency:** action tuples (`create_broadcast`/`go_live`/`end_broadcast`/`delete_broadcast`/`redirect_online`/`redirect_offline`) are identical across `reconcile.py` (Task 4) and `execute()` (Task 8). `owned_broadcasts`/`life`/`is_recent` signatures match between Tasks 2, 4, 8. `get_owned_stream_id` name consistent (renamed from `get_production_stream_id`).

**Note on test runner:** `uv run --with pytest python -m pytest` (pytest isn't in system Python; `uv` is the repo convention). Only `redirect.py` touches `boto3`, lazily, and its tests inject fakes — so no task needs `boto3` installed to run its tests.
