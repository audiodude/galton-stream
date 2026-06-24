# Box-side Playout & Health — Design Spec

**Date:** 2026-06-24
**Status:** Approved design — ready for implementation plan.
**Box:** Hetzner `radio-playout` (5.78.198.233, Ubuntu 24.04), repo checked out at `/opt/radio`.
**Related:** [2026-06-17-new-monitor-design.md](2026-06-17-new-monitor-design.md) (defines the box-side contract this fulfills), [2026-06-16-prebaked-playout-design.md](2026-06-16-prebaked-playout-design.md) (the bake/playout pipeline). Supersedes that plan's held Task 7 (whose "run monitor.py on the box" assumption is obsolete — the monitor now runs on Railway).

## Goal

Make the box autonomously fulfill the production side of the vxstory station:

1. **Bake** the day's show on a schedule before the window.
2. **Playout** the baked show, window-gated, pushing `-re -c copy` to YouTube **only** during the operational window (11:45–18:05 PT), self-healing, idle outside it.
3. **Health** — expose a token-authed `/health` endpoint (via a Cloudflare tunnel) that the Railway `streaming-monitor` polls.

The monitor owns broadcast lifecycle + the radio redirect; the box only produces a clean stream and reports health. The daily stream `inactive→active` edge (box starts at window open) and `active→inactive` edge (box stops at close) are what drive the monitor's go-live and teardown.

## Context / what's already true

- The new monitor is deployed on Railway as `streaming-monitor` (currently suspended). Its `boxhealth.probe()` does `GET BOX_HEALTH_URL` with `Authorization: Bearer BOX_HEALTH_TOKEN`, 8 s timeout, and treats **any 200 as "reachable"** (it does not inspect the body — fields are for human observability). Box health is a DEGRADED-alert *enrichment only*; it never gates lifecycle.
- The monitor's operational window is `11:45–18:05` PT and consumer window `12:00–18:00` PT (`monitor/windows.py`, America/Los_Angeles, half-open). **The box must gate on the same operational window.**
- Stream key matches: the box's `/etc/radio.env` `YOUTUBE_STREAM_KEY` == `streaming-monitor`'s, resolving liveStream `…Zg1781149616432484`.
- On the box now: `scripts/bake/{plan,assemble,validate}.py` + `bake.sh`, `scripts/broadcast/{playout.sh,health_server.py}` (the health server is the old galton one; `playout.sh` loops with **no window gate**). No systemd units / timers / cron exist. `cloudflared` is authed and `CF_API_TOKEN` is available.

## Shared contract (the one cross-component invariant)

The operational window `11:45–18:05 America/Los_Angeles` (half-open) is the single coordination point between the box and the monitor. It is duplicated in two places — `monitor/windows.py` and the box's `playout.sh` gate — and each must carry a comment naming the other as the source of truth. If they drift, the stream can go active outside the monitor's window and never get a `go_live`.

## Architecture — three independent units

### 1. Bake — `radio-bake.timer` + `radio-bake.service`
- **systemd timer** (chosen over cron: journald logging, `Persistent=true` catch-up after downtime, TZ-aware `OnCalendar`). Fires **`OnCalendar=*-*-* 03:00:00 America/Los_Angeles`** — deep inside the 18:05→11:45 idle gap.
- `radio-bake.service` (Type=oneshot, `EnvironmentFile=/etc/radio.env`) runs `scripts/bake/bake.sh`: S3 sync (music + catalog) → `plan → assemble → validate` → atomic `show-YYYY-MM-DD.mkv` + update `LATEST` → prune by `RETENTION_DAYS`.
- `SHOW_DUR ≈ 22800` (operational-window length) so one play-through covers the window. *(Content variety from today's 4-piece catalog is out of scope — tracked separately.)*
- Bake-fit verified ample: the 45-min test baked at 2.71× realtime → a ~6 h20 m 720p60 show ≈ 2.3 h, well inside the gap. Re-measure when the catalog moves to 1080p60.
- **On failure:** `LATEST` is untouched (atomic), so playout keeps the prior show; send a Telegram alert (reuses `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` already in env).

### 2. Playout — `radio-playout.service`
- One systemd service, `Restart=always`, `EnvironmentFile=/etc/radio.env`, `ExecStart=scripts/broadcast/playout.sh`.
- **Window gate lives inside `playout.sh`** (chosen over systemd start/stop timers: one unit, mirrors galton-stream's proven `start.sh`, gate is ~10 lines). Loop:
  - **Out of window:** ensure no ffmpeg child is running (kill + wait if transitioning out), sleep ~30 s, recheck.
  - **In window:** stream `LATEST` via `-re -c copy` to `$YOUTUBE_URL/$YOUTUBE_STREAM_KEY` (existing behavior); on ffmpeg exit, restart after a short delay (existing self-heal). Touch `$PLAYOUT_HEARTBEAT` every ~5 s while streaming (existing).
  - At **18:05** the gate detects out-of-window and kills ffmpeg → stream goes inactive → the monitor (now out-of-window) completes the broadcast. Edge granularity = the ~30 s recheck interval, absorbed by the 18:00–18:05 cooldown.
- Window check: `TZ=America/Los_Angeles` integer compare `1145 <= HHMM < 1805` (DST-correct via the tz database; window never crosses midnight). Comment cross-references `monitor/windows.py`.

### 3. Health — `radio-health.service` + `cloudflared.service`
- Rewrite `scripts/broadcast/health_server.py` for pre-baked playout:
  - Requires header `Authorization: Bearer $BOX_HEALTH_TOKEN` → else **401**.
  - On valid token → **200** + JSON `{"playout_alive": bool, "ffmpeg_alive": bool, "heartbeat_age_s": int}`.
    - `ffmpeg_alive`: `pgrep -x ffmpeg` non-empty.
    - `heartbeat_age_s`: `now - mtime($PLAYOUT_HEARTBEAT)`, or `-1` if the file is absent.
    - `playout_alive`: `true` when in-window and ffmpeg is up, **or** when out-of-window (idle by design); `false` only when in-window with no ffmpeg/stale heartbeat.
  - Binds **`127.0.0.1:$BOX_HEALTH_PORT`** only — no public port is ever opened on the box.
- **cloudflared** named tunnel (systemd `cloudflared.service`) with ingress `radio-sys.dangerthirdrail.com → http://localhost:$BOX_HEALTH_PORT`, plus the zone DNS record routing that hostname to the tunnel. `radio-sys.dangerthirdrail.com` is a single-level subdomain (covered by Cloudflare's free Universal SSL; a deeper name would need Advanced Certificate Manager).
- Net exposure: the only inbound path to `/health` is the authenticated Cloudflare tunnel over HTTPS; the bearer token authenticates the request and is TLS-protected in transit.

## Env / secret coordination
- Generate `BOX_HEALTH_TOKEN` once (`openssl rand -hex 32`).
- Box `/etc/radio.env` gains: `BOX_HEALTH_TOKEN`, `BOX_HEALTH_PORT` (e.g. 8088).
- Railway `streaming-monitor` gains: `BOX_HEALTH_URL=https://radio-sys.dangerthirdrail.com/health` and `BOX_HEALTH_TOKEN` (same value), set via `variableUpsert` with the value passed through a shell var (never echoed to logs/transcript).

## Deployment / code management
- `/opt/radio` tracks **`release`** (the production branch Railway also deploys the monitor from). Update = `git pull` + `systemctl restart radio-playout radio-health` (+ the timer reloads on next fire). Box-side script changes flow `main → release` like the monitor.
- The full checkout stays; the old galton scripts are inert (not referenced by any unit). Retiring them is the separate prebaked-playout Task 8.

## Error handling
- **Bake fail:** stale-but-serving (prior `LATEST`), Telegram alert. Non-fatal to uptime.
- **Playout:** self-heals via the loop; missing `LATEST` → "no show, retry 15 s."
- **Health/tunnel down:** the monitor sees the box as unreachable → DEGRADED *enrichment only*, never a teardown. `cloudflared.service`, `radio-health.service`, `radio-playout.service` are all `Restart=always`.

## Testing
- **Unit (pytest, repo pattern in `scripts/.../tests/`):**
  - Window-gate decision function — pure: in/out given pinned PT times, including a DST date (e.g. a summer and a winter date) and the 11:45 / 18:05 boundaries.
  - Health server — no/invalid token → 401; valid token → 200 + the three-field JSON, with `ffmpeg_alive`/`heartbeat_age_s` derived from a fixture heartbeat file (fresh, stale, absent).
- **Integration — the go-live gate (manual, during a window or `FORCE_ACTIVE=1`):** with a baked show present and the monitor resumed, confirm: stream goes active → monitor transitions the broadcast live → `radio.dangerthirdrail.com` redirect flips online at 12:00 → broadcast survives an ffmpeg restart without auto-completing → video cuts land on song boundaries → at 18:05 the box stops and the monitor completes the broadcast. This is the success gate before declaring the box-side done.

## Decisions locked
- Health exposure: **Cloudflare tunnel** (no open inbound port; HTTPS; token in transit protected) at **`radio-sys.dangerthirdrail.com`**.
- Scheduling: **systemd timer** for the nightly bake (03:00 PT).
- Window gating: **inside `playout.sh`** (self-gate, `TZ=America/Los_Angeles`), matching `monitor/windows.py`'s 11:45–18:05.
- Health server binds **localhost**; only cloudflared reaches it.

## Out of scope (tracked elsewhere)
- Catalog content variety (only 4 pieces today → a 6 h show is repetitive) — needs more renders + EDL in-point advance.
- Retiring the superseded galton live-mux scripts (prebaked-playout Task 8).
- 1080p60 (re-measure bake-fit when the catalog is re-rendered).
