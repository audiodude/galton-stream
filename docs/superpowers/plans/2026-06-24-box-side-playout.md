# Box-side Playout & Health — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development for **Part A (code, Tasks 1–3)**. **Part B (deployment, Tasks 4–9) is operator-run on the live box** — execute directly with verification, NOT via code subagents. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the box autonomously bake nightly, stream the show window-gated (11:45–18:05 PT), and serve a token-authed `/health` exposed via a Cloudflare tunnel for the Railway monitor.

**Architecture:** Three systemd-managed units on `radio-playout` (bake timer, playout service, health service) plus a cloudflared tunnel. The window gate is a tiny Python module `window.py` (mirrors `monitor/windows.py`) that `playout.sh` calls; the health server is token-authed and localhost-bound.

**Tech Stack:** bash, Python 3 stdlib (`http.server`, `zoneinfo`), systemd, cloudflared. Tests: `pytest` via `uv run --with pytest python -m pytest <path> -v`.

## Global Constraints

- **Shared window invariant:** the box's operational window MUST equal `monitor/windows.py`'s: `11:45–18:05 America/Los_Angeles`, half-open `[start,end)`. Duplicated in `scripts/broadcast/window.py`; each carries a comment naming the other as source of truth.
- **Health contract:** `GET /health` with `Authorization: Bearer $BOX_HEALTH_TOKEN` → `200` + JSON `{playout_alive, ffmpeg_alive, heartbeat_age_s}`; missing/invalid/empty token → `401`. Server binds `127.0.0.1:$BOX_HEALTH_PORT` only. (The monitor treats any 200 as "reachable".)
- **Exposure:** `/health` reachable ONLY via the cloudflared tunnel at `https://radio-sys.dangerthirdrail.com/health` (single-level subdomain → free Universal SSL). No inbound port opened on the box.
- **Deployment branch:** `/opt/radio` tracks **`release`** (same branch Railway deploys the monitor from). Box-side code flows `main → release`, then `git pull` on the box.
- Tests live in `scripts/broadcast/tests/`, mirroring `scripts/bake/tests/` (prepend parent dir to `sys.path`).

---

# Part A — Code (subagent-driven TDD, feature branch `box-side`)

### Task 1: `window.py` — operational-window gate

**Files:**
- Create: `scripts/broadcast/window.py`
- Test: `scripts/broadcast/tests/test_window.py`

**Interfaces:**
- Produces: `in_operational_window(now=None) -> bool`; `__main__` exits `0` if in window, `1` if not (so `playout.sh` can do `python3 window.py && …`).

- [ ] **Step 1: Write the failing test**

```python
# scripts/broadcast/tests/test_window.py
import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import window
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
def at(mon, day, h, m): return datetime.datetime(2026, mon, day, h, m, tzinfo=PT)

def test_boundaries_dst_summer():           # June = PDT
    assert not window.in_operational_window(at(6, 24, 11, 44))
    assert window.in_operational_window(at(6, 24, 11, 45))   # inclusive start
    assert window.in_operational_window(at(6, 24, 18, 4))
    assert not window.in_operational_window(at(6, 24, 18, 5)) # exclusive end

def test_boundaries_standard_winter():      # January = PST
    assert not window.in_operational_window(at(1, 15, 11, 44))
    assert window.in_operational_window(at(1, 15, 12, 0))
    assert not window.in_operational_window(at(1, 15, 18, 5))
```

- [ ] **Step 2: Run test, verify it fails** — `uv run --with pytest python -m pytest scripts/broadcast/tests/test_window.py -v` → FAIL `No module named 'window'`.

- [ ] **Step 3: Write the implementation**

```python
#!/usr/bin/env python3
"""Operational-window gate for box playout. MIRRORS monitor/windows.py's
operational window (11:45-18:05 America/Los_Angeles, half-open) — KEEP IN SYNC.
CLI: exit 0 if in window, 1 if not (used by playout.sh: `python3 window.py && stream`)."""
import datetime
import sys
from zoneinfo import ZoneInfo

ACTIVE_TZ = ZoneInfo("America/Los_Angeles")
OPERATIONAL_START = (11, 45)
OPERATIONAL_END = (18, 5)


def in_operational_window(now=None):
    now = now or datetime.datetime.now(ACTIVE_TZ)
    return OPERATIONAL_START <= (now.hour, now.minute) < OPERATIONAL_END


if __name__ == "__main__":
    sys.exit(0 if in_operational_window() else 1)
```

- [ ] **Step 4: Run test, verify it passes** — PASS (2 passed).
- [ ] **Step 5: Commit** — `git add scripts/broadcast/window.py scripts/broadcast/tests/test_window.py && git commit -m "feat(broadcast): window.py operational-window gate (mirrors monitor/windows.py)"`

---

### Task 2: window-gate `playout.sh`

**Files:**
- Modify (rewrite): `scripts/broadcast/playout.sh`
- Test: `scripts/broadcast/tests/test_playout_gate.py`

**Interfaces:**
- Consumes: `window.py` (Task 1) via `WINDOW_CMD` (default `python3 $HERE/window.py`).
- Produces: window-gated streaming. Injectable seams for tests: `WINDOW_CMD`, `FFMPEG_BIN`, `SUPERVISE_INTERVAL`, `SHOWS_DIR`, `PLAYOUT_HEARTBEAT`.

- [ ] **Step 1: Write the failing test** (stub-based integration; tolerant timing)

```python
# scripts/broadcast/tests/test_playout_gate.py
import os, sys, subprocess, tempfile, textwrap, time, signal, pathlib

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYOUT = os.path.join(os.path.dirname(HERE), "playout.sh")

def _run_playout(env, secs=4):
    p = subprocess.Popen(["bash", PLAYOUT], env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
    time.sleep(secs)
    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
    try: p.wait(timeout=10)
    except subprocess.TimeoutExpired: os.killpg(os.getpgid(p.pid), signal.SIGKILL)

def _base_env(tmp, in_window: bool):
    shows = pathlib.Path(tmp) / "shows"; shows.mkdir()
    (shows / "show-x.mkv").write_text("fake")
    (shows / "LATEST").write_text("show-x.mkv")
    marker = pathlib.Path(tmp) / "ff_ran"
    fake_ff = pathlib.Path(tmp) / "ffmpeg"
    fake_ff.write_text("#!/usr/bin/env bash\ntouch '%s'\nsleep 30\n" % marker)
    fake_ff.chmod(0o755)
    env = dict(os.environ)
    env.update({
        "SHOWS_DIR": str(shows), "PLAYOUT_HEARTBEAT": str(pathlib.Path(tmp) / "hb"),
        "YOUTUBE_STREAM_KEY": "k", "YOUTUBE_URL": "rtmp://example/live",
        "FFMPEG_BIN": str(fake_ff), "SUPERVISE_INTERVAL": "1",
        "WINDOW_CMD": "true" if in_window else "false",
    })
    return env, marker

def test_out_of_window_does_not_start_ffmpeg():
    with tempfile.TemporaryDirectory() as tmp:
        env, marker = _base_env(tmp, in_window=False)
        _run_playout(env, secs=3)
        assert not marker.exists()   # ffmpeg never invoked outside the window

def test_in_window_starts_ffmpeg():
    with tempfile.TemporaryDirectory() as tmp:
        env, marker = _base_env(tmp, in_window=True)
        _run_playout(env, secs=3)
        assert marker.exists()       # ffmpeg invoked inside the window
```

- [ ] **Step 2: Run test, verify it fails** — current `playout.sh` ignores `WINDOW_CMD`/`FFMPEG_BIN`, so `test_out_of_window_does_not_start_ffmpeg` FAILS (it streams regardless). Run: `uv run --with pytest python -m pytest scripts/broadcast/tests/test_playout_gate.py -v`.

- [ ] **Step 3: Rewrite `scripts/broadcast/playout.sh`**

```bash
#!/usr/bin/env bash
# Live playout: stream-copy the day's pre-baked show to YouTube RTMP, GATED to the
# operational window (11:45-18:05 PT — see window.py / monitor/windows.py).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SHOWS_DIR="${SHOWS_DIR:-/data/shows}"
YOUTUBE_URL="${YOUTUBE_URL:-rtmp://a.rtmp.youtube.com/live2}"
HEARTBEAT="${PLAYOUT_HEARTBEAT:-/tmp/playout_heartbeat}"
WINDOW_CMD="${WINDOW_CMD:-python3 $HERE/window.py}"   # exit 0 == in operational window
FFMPEG_BIN="${FFMPEG_BIN:-ffmpeg}"
SUPERVISE_INTERVAL="${SUPERVISE_INTERVAL:-15}"

resolve_show() {
    local latest="$SHOWS_DIR/LATEST"
    if [[ -f "$latest" ]] && [[ -f "$SHOWS_DIR/$(cat "$latest")" ]]; then
        echo "$SHOWS_DIR/$(cat "$latest")"; return
    fi
    ls -t "$SHOWS_DIR"/show-*.mkv 2>/dev/null | head -1
}

FFPID=""; HB=""
stop_ff() { [[ -n "${FFPID:-}" ]] && kill -TERM "$FFPID" 2>/dev/null && wait "$FFPID" 2>/dev/null; FFPID=""; }
stop_hb() { [[ -n "${HB:-}" ]] && kill "$HB" 2>/dev/null; HB=""; }
cleanup() { stop_ff; stop_hb; exit 0; }
trap cleanup SIGTERM SIGINT
in_window() { $WINDOW_CMD; }

while true; do
    if ! in_window; then stop_ff; stop_hb; sleep "$SUPERVISE_INTERVAL"; continue; fi
    SHOW="$(resolve_show)"
    if [[ -z "$SHOW" || ! -f "$SHOW" ]]; then
        echo "[playout] no show available; retrying in 15s" >&2; sleep 15; continue
    fi
    echo "[playout] streaming $SHOW" >&2
    ( while :; do touch "$HEARTBEAT"; sleep 5; done ) & HB=$!
    setsid "$FFMPEG_BIN" -hide_banner -loglevel warning -re -i "$SHOW" \
        -c copy -f flv "$YOUTUBE_URL/${YOUTUBE_STREAM_KEY:?}" &
    FFPID=$!
    # supervise: kill ffmpeg if the window closes; otherwise wait for it to exit
    while kill -0 "$FFPID" 2>/dev/null; do
        if ! in_window; then echo "[playout] window closed; stopping ffmpeg" >&2; stop_ff; break; fi
        sleep "$SUPERVISE_INTERVAL"
    done
    if [[ -n "${FFPID:-}" ]]; then   # ffmpeg exited on its own (window still open)
        wait "$FFPID" 2>/dev/null || true; FFPID=""; stop_hb
        echo "[playout] ffmpeg exited; restart in 3s" >&2; sleep 3
    else                              # we killed it on window close
        stop_hb
    fi
done
```

- [ ] **Step 4: Run test, verify it passes** — both pass.
- [ ] **Step 5: Commit** — `git add scripts/broadcast/playout.sh scripts/broadcast/tests/test_playout_gate.py && git commit -m "feat(broadcast): window-gate playout.sh (stream only in operational window)"`

---

### Task 3: token-authed `health_server.py`

**Files:**
- Modify (rewrite): `scripts/broadcast/health_server.py`
- Test: `scripts/broadcast/tests/test_health_server.py`

**Interfaces:**
- Produces: `health_payload(ffmpeg_alive_fn, heartbeat_age_fn, in_window_fn) -> dict` (testable, injectable); HTTP handler enforcing the Global-Constraints health contract.

- [ ] **Step 1: Write the failing test**

```python
# scripts/broadcast/tests/test_health_server.py
import os, sys, json, threading, urllib.request, urllib.error, http.server
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import health_server as hs

def test_payload_in_window_streaming():
    p = hs.health_payload(lambda: True, lambda: 2, lambda: True)
    assert p == {"playout_alive": True, "ffmpeg_alive": True, "heartbeat_age_s": 2}

def test_payload_in_window_ffmpeg_down():
    p = hs.health_payload(lambda: False, lambda: 99, lambda: True)
    assert p["playout_alive"] is False and p["ffmpeg_alive"] is False

def test_payload_out_of_window_is_idle_ok():
    p = hs.health_payload(lambda: False, lambda: -1, lambda: False)
    assert p["playout_alive"] is True and p["heartbeat_age_s"] == -1

def _serve(monkeypatch_token):
    hs.TOKEN = monkeypatch_token
    srv = http.server.HTTPServer(("127.0.0.1", 0), hs.H)
    threading.Thread(target=srv.handle_request, daemon=True)  # one per call below
    return srv

def _get(srv, auth=None):
    port = srv.server_address[1]
    req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
    if auth: req.add_header("Authorization", auth)
    t = threading.Thread(target=srv.handle_request); t.start()
    try:
        r = urllib.request.urlopen(req, timeout=5); t.join(); return r.status, r.read()
    except urllib.error.HTTPError as e:
        t.join(); return e.code, e.read()

def test_http_401_without_token():
    srv = _serve("secret"); code, _ = _get(srv); assert code == 401

def test_http_401_wrong_token():
    srv = _serve("secret"); code, _ = _get(srv, "Bearer nope"); assert code == 401

def test_http_200_with_token():
    srv = _serve("secret")
    code, body = _get(srv, "Bearer secret")
    assert code == 200
    d = json.loads(body); assert set(d) == {"playout_alive", "ffmpeg_alive", "heartbeat_age_s"}
```

- [ ] **Step 2: Run test, verify it fails** — current server has no `health_payload`/`TOKEN`/auth → FAIL. Run: `uv run --with pytest python -m pytest scripts/broadcast/tests/test_health_server.py -v`.

- [ ] **Step 3: Rewrite `scripts/broadcast/health_server.py`**

```python
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
```

- [ ] **Step 4: Run test, verify it passes** — all pass.
- [ ] **Step 5: Run the whole broadcast suite** — `uv run --with pytest python -m pytest scripts/broadcast/tests/ -v` → all green.
- [ ] **Step 6: Commit** — `git add scripts/broadcast/health_server.py scripts/broadcast/tests/test_health_server.py && git commit -m "feat(broadcast): token-authed localhost /health for pre-baked playout"`

---

# Part B — Deployment (operator-run on the live box; NOT subagent code tasks)

> Prereq: Part A merged `box-side` → `main` → `release` (FF), pushed. The box pulls `release`.

### Task 4: secrets + env coordination

- [ ] **Generate the token once (no echo) and write to the box `/etc/radio.env`:**
  ```bash
  TOKEN=$(openssl rand -hex 32)
  ssh radio-playout "grep -q '^BOX_HEALTH_TOKEN=' /etc/radio.env || printf 'BOX_HEALTH_TOKEN=%s\nBOX_HEALTH_PORT=8088\n' '$TOKEN' >> /etc/radio.env"
  ```
  (Value flows via the `$TOKEN` shell var only — never printed.)
- [ ] **Set the same token + URL on Railway `streaming-monitor`** (`variableUpsert`, value via shell var, never echoed). Read the token back from the box so both sides match exactly, then upsert `BOX_HEALTH_TOKEN` and `BOX_HEALTH_URL=https://radio-sys.dangerthirdrail.com/health`. Verify by sha256 that box and Railway hold the same token.

### Task 5: deploy code to the box

- [ ] On the box: `cd /opt/radio && git fetch origin && git checkout release && git pull --ff-only origin release`. Confirm `scripts/broadcast/window.py` and the rewritten `health_server.py`/`playout.sh` are present (`git log -1 --oneline`).

### Task 6: systemd units

- [ ] **`/etc/systemd/system/radio-health.service`:**
  ```ini
  [Unit]
  Description=radio box token-authed health endpoint (localhost)
  After=network.target
  [Service]
  EnvironmentFile=/etc/radio.env
  ExecStart=/usr/bin/python3 /opt/radio/scripts/broadcast/health_server.py
  Restart=always
  RestartSec=3
  [Install]
  WantedBy=multi-user.target
  ```
- [ ] **`/etc/systemd/system/radio-playout.service`:**
  ```ini
  [Unit]
  Description=radio window-gated playout (-re -c copy to YouTube)
  After=network.target
  [Service]
  EnvironmentFile=/etc/radio.env
  ExecStart=/usr/bin/env bash /opt/radio/scripts/broadcast/playout.sh
  Restart=always
  RestartSec=5
  KillMode=mixed
  TimeoutStopSec=20
  [Install]
  WantedBy=multi-user.target
  ```
- [ ] **`/etc/systemd/system/radio-bake.service`:**
  ```ini
  [Unit]
  Description=radio nightly bake
  After=network.target
  [Service]
  Type=oneshot
  EnvironmentFile=/etc/radio.env
  ExecStart=/usr/bin/env bash /opt/radio/scripts/bake/bake.sh
  ```
- [ ] **`/etc/systemd/system/radio-bake.timer`:**
  ```ini
  [Unit]
  Description=radio nightly bake (03:00 PT)
  [Timer]
  OnCalendar=*-*-* 03:00:00 America/Los_Angeles
  Persistent=true
  [Install]
  WantedBy=timers.target
  ```
- [ ] `systemctl daemon-reload && systemctl enable --now radio-health.service radio-playout.service radio-bake.timer`
- [ ] Verify: `systemctl is-active radio-health radio-playout`; `systemctl list-timers radio-bake.timer`; `curl -s localhost:8088/health` → 401 (no token); `curl -s -H "Authorization: Bearer $TOKEN" localhost:8088/health` → 200 JSON. (Outside the window, `radio-playout` logs the window-sleep and starts no ffmpeg — expected.)

### Task 7: cloudflared tunnel (host-authed create → box runs it)

> The host has an authed `cloudflared` (cert.pem); the box does not. Create the tunnel + DNS from the host, copy creds to the box, run there.

- [ ] **On the host:** `cloudflared tunnel create radio-sys` → note the tunnel UUID + the credentials JSON path (`~/.cloudflared/<UUID>.json`). Then `cloudflared tunnel route dns radio-sys radio-sys.dangerthirdrail.com` (creates the proxied CNAME in the `dangerthirdrail.com` zone).
- [ ] **Install cloudflared on the box** (if absent): download the official `cloudflared` binary to `/usr/local/bin/cloudflared`.
- [ ] **Copy creds to the box:** `scp ~/.cloudflared/<UUID>.json radio-playout:/root/.cloudflared/<UUID>.json` (`ssh radio-playout 'mkdir -p /root/.cloudflared'` first).
- [ ] **`/etc/cloudflared/config.yml` on the box:**
  ```yaml
  tunnel: <UUID>
  credentials-file: /root/.cloudflared/<UUID>.json
  ingress:
    - hostname: radio-sys.dangerthirdrail.com
      service: http://localhost:8088
    - service: http_status:404
  ```
- [ ] **`/etc/systemd/system/cloudflared.service` on the box:**
  ```ini
  [Unit]
  Description=cloudflared tunnel (radio-sys health)
  After=network.target
  [Service]
  ExecStart=/usr/local/bin/cloudflared --config /etc/cloudflared/config.yml tunnel run
  Restart=always
  RestartSec=5
  [Install]
  WantedBy=multi-user.target
  ```
- [ ] `systemctl daemon-reload && systemctl enable --now cloudflared` ; `systemctl is-active cloudflared`; box logs show the tunnel registered.

### Task 8: verify the public health path (off-box)

- [ ] From the host (DNS may take ~1 min to propagate + cert issue):
  ```bash
  curl -s -o /dev/null -w '%{http_code}\n' https://radio-sys.dangerthirdrail.com/health            # expect 401
  curl -s -H "Authorization: Bearer $TOKEN" https://radio-sys.dangerthirdrail.com/health           # expect 200 + JSON
  ```
- [ ] Confirm TLS is valid (no cert error) — proves the single-level subdomain is covered.

### Task 9: live go-live gate **(WINDOW-ONLY: 11:45–18:05 PT)**

> Do this only during the operational window, with a baked show present (`ssh radio-playout 'ls -lh /data/shows/'`; run `radio-bake.service` once by hand if needed).

- [ ] Resume the monitor: `deploymentRedeploy` the `streaming-monitor` service.
- [ ] Confirm, within ~2 min: the box's stream goes `active` → monitor transitions the broadcast to `live` → `radio.dangerthirdrail.com` redirects to the live video (in consumer window) → the broadcast survives an ffmpeg/show-loop restart without auto-completing → video cuts land on song boundaries.
- [ ] At/after **18:05 PT**: box stops streaming → stream `inactive` → monitor completes the broadcast → redirect goes offline. This is the success gate.

---

## Self-Review

**Spec coverage:** bake timer (Task 6) ✅; window-gated playout (Tasks 1,2,6) ✅; token-authed localhost /health (Task 3,6) ✅; cloudflared tunnel @ radio-sys (Task 7) ✅; BOX_HEALTH_TOKEN/URL coordination (Task 4) ✅; release-branch deploy (Task 5) ✅; shared-window invariant (Tasks 1,3 cross-ref) ✅; error handling = Restart=always units + bake atomic LATEST ✅; testing = window + health unit tests (1,3) + playout gate integration (2) + live gate (9) ✅. Bake Telegram-on-failure from the spec is **deferred** (noted): bake.sh already exits non-zero on failure and leaves LATEST intact; a Telegram hook is a small follow-up, not gating — flag at final review.

**Placeholder scan:** `<UUID>` in Task 7 is a runtime value the operator fills from `cloudflared tunnel create` output (not a plan gap). No TBD/TODO elsewhere.

**Consistency:** `BOX_HEALTH_PORT=8088`, `radio-sys.dangerthirdrail.com`, `BOX_HEALTH_TOKEN`, window `11:45–18:05` are identical across all tasks and match the spec + `monitor/boxhealth.py` contract.
