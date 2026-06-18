# New Monitor (vxstory station) — Design Spec

**Date:** 2026-06-17
**Status:** Approved design — ready for implementation plan.
**Supersedes:** the original galton monitor (`monitor/monitor.py`), tagged
`galton-monitor-orig` (`3639d2a`) for reference.
**Related:** [2026-06-17-monitor-broadcast-coordination.md](2026-06-17-monitor-broadcast-coordination.md)
(root-causes the unscoped reaper this spec fixes),
[2026-06-16-prebaked-playout-design.md](2026-06-16-prebaked-playout-design.md)
(the box-side pre-baked playout this monitor observes).

## Goal

A windowed YouTube broadcast-lifecycle monitor, running on Railway, for the
relaunched **vxstory** station whose playout runs on the Hetzner box
`radio-playout` (5.78.198.233). It observes the box, owns the YouTube broadcast
lifecycle, drives the `radio.dangerthirdrail.com` redirect, and alerts on
trouble — **without ever touching a broadcast it does not own.**

## Context (what changed)

- **galton-stream is decommissioned.** The video source is now the Hetzner box
  running vxstory pre-baked playout (`-re -c copy` of a baked daily show),
  proven end-to-end in the 2026-06-17 live test.
- galton-stream was a **Railway** service, reachable via Railway internal DNS,
  and the old monitor could restart it via the Railway API. The box is
  **external**; that lever is gone.
- The old monitor also ran galton-specific subsystems (`chat_poller`,
  `title_writer`) that vxstory does not need (titles are baked into the show;
  there is no live chat interaction).

## The invariant that fixes the bug

The old monitor's out-of-window teardown completed **every** `live`/`testing`
broadcast on the channel, unscoped — which repeatedly reaped the vxstory test
broadcasts. The new monitor enforces one rule everywhere:

> **The monitor only ever creates, binds, transitions, completes, or
> privatizes broadcasts whose `contentDetails.boundStreamId` equals its own
> production stream id** — the liveStream whose `cdn.ingestionInfo.streamName`
> equals the configured `YOUTUBE_STREAM_KEY`.

Every list of broadcasts passes through a single ownership filter
(`owned_broadcasts(stream_id)`) before any mutating action. A foreign live
broadcast on the same channel is always a no-op for this monitor.

## Control model (decided)

**Observe + YouTube lifecycle.** The box self-schedules and self-heals playout;
the monitor never issues control commands into the box. "Talk to the box" means
**probe its `/health` endpoint**. No inbound control path into the box exists.

## Architecture

Railway service (reuse the existing `galton-monitor` service; renaming to
`vxstory-monitor` is optional) runs a single reconcile loop polling every
`POLL_INTERVAL` seconds. Code is split into focused modules under `monitor/`:

```
monitor/
  windows.py    # pure window math (no I/O)
  youtube.py    # OAuth + API wrapper + owned-stream lifecycle
  redirect.py   # S3/CloudFront radio redirect
  boxhealth.py  # probe the box /health (diagnostic)
  alerts.py     # Telegram, edge-triggered
  monitor.py    # the reconcile loop wiring it together
```

Each module has one responsibility, a small interface, and is testable in
isolation.

### `windows.py`

Pure functions over a `now` (America/Los_Angeles):

- `in_operational_window(now) -> bool` — default 11:45–18:05 PT.
- `in_consumer_window(now) -> bool` — default 12:00–18:00 PT.
- `next_edge(now) -> float` — seconds to the next of the four window edges
  (used to size sleeps and pre-empt boundaries).

No network or clock side effects beyond reading `now`; the caller passes the
current time so tests can pin it. The window constants are the **shared source
of truth** with the box's playout scheduling (see Box-side contract).

### `youtube.py`

OAuth refresh-token access tokens (cached until ~60 s before expiry) and a thin
authenticated request wrapper that never raises into the loop (logs + returns
`None` on HTTP/transport error). On top of that, the owned-stream lifecycle:

- `get_owned_stream_id() -> str | None` — list `liveStreams?mine=true`, return
  the id whose `cdn.ingestionInfo.streamName == YOUTUBE_STREAM_KEY`. Cached for
  the process lifetime (the key→id mapping is stable).
- `owned_broadcasts(stream_id) -> list[dict]` — list active+upcoming
  broadcasts, filtered to `boundStreamId == stream_id`. **The ownership lens.**
- `stream_status(stream_id) -> tuple[str, str]` — `(streamStatus,
  healthStatus)` from the `liveStreams` API, e.g. `("active", "good")`. The
  source of truth for "is the show reaching YouTube."
- `ensure_broadcast(stream_id) -> str | None` — if no owned live-or-recent
  broadcast exists, create one (title/description from config,
  `privacyStatus` from config, `enableAutoStart=False`, `enableAutoStop=False`,
  `latencyPreference=ultraLow`, `enableDvr=False`), bind it to `stream_id`,
  return its id. Idempotent: returns the existing owned broadcast id if one is
  already live/recent.
- `go_live(broadcast_id) -> bool` — transition `testing`/`ready` → `live`.
  Called only when the bound stream is `active`.
- `end_broadcast(broadcast_id) -> bool` — transition → `complete`; optionally
  set privacy private. Called only on owned broadcasts.
- `delete_stale_owned(stream_id)` — delete owned `created`/`ready`/`upcoming`
  broadcasts older than the recency window, so a fresh create+bind wins (scoped
  version of the existing stale cleanup).

`enableAutoStop=False` is deliberate: a brief ffmpeg restart or box reboot must
not end the broadcast (proven valuable in the live test, where the show-loop
restart rode through without dropping).

### `redirect.py`

Lifted from the current monitor with no behavior change:

- `current_video_id() -> str | None` — read the bucket website routing rule.
- `set_radio_online(video_id)` — put a routing rule 302-redirecting to
  `www.youtube.com/live/<video_id>`, then invalidate CloudFront.
- `set_radio_offline()` — drop the routing rule (serve `index.html` offline
  card), then invalidate CloudFront.

All redirects are **302** (never 301 — 301s are cached indefinitely by
browsers).

### `boxhealth.py`

- `probe() -> dict | None` — GET `BOX_HEALTH_URL` with header
  `Authorization: Bearer BOX_HEALTH_TOKEN`, short timeout. Returns the parsed
  health JSON (`playout_alive`, `ffmpeg_alive`, `heartbeat_age_s`) or `None` if
  unreachable.

This is a **diagnostic enrichment only** — it never gates a lifecycle
decision. It lets alerts distinguish "box process dead" from "YouTube glitch."

### `alerts.py`

- `notify(message)` — send a Telegram message.
- Edge-triggered incident discipline carried over from the old
  `on_state_transition`: page on the problem edge and again on recovery; do not
  page routine daily churn (window open/close).

### `monitor.py` (reconcile loop)

```
while True:
    now = current_time()
    stream_id = get_owned_stream_id()
    if stream_id is None:
        alert_once("cannot resolve production stream"); sleep(POLL_INTERVAL); continue
    owned = owned_broadcasts(stream_id)
    live  = first owned with lifeCycleStatus in (live, testing)
    ss, health = stream_status(stream_id)
    reconcile(now, stream_id, owned, live, ss, health)
    sleep(POLL_INTERVAL)
```

### Reconcile logic

**In operational window:**
1. `delete_stale_owned(stream_id)`.
2. If no owned live-or-recent broadcast → `ensure_broadcast(stream_id)`.
3. If an owned broadcast is `ready`/`testing` **and** `ss == active` →
   `go_live(bid)`.
4. Redirect: if `in_consumer_window(now)` **and** owned `live` broadcast
   **and** `ss == active` → `set_radio_online(vid)`; else `set_radio_offline()`.
5. Failure (ride it out): if the stream is not `active`/healthy, **keep** the
   broadcast (no teardown), ensure radio offline, alert on the problem edge.
   On stream return, restore the redirect and send a recovery alert.

**Outside operational window:**
1. For each **owned** broadcast in (`live`, `testing`) → `end_broadcast(bid)` +
   set private. **Foreign broadcasts are never touched.**
2. Ensure radio offline.

### Go-live without a control channel

`enableAutoStart` is **dropped**. YouTube's auto-start fires only on a stream
`inactive→active` edge with a broadcast already bound at that instant; for a
self-scheduling remote box we can neither guarantee the bind precedes the edge
nor "bounce" ffmpeg to re-trigger it. Instead the monitor explicitly
transitions `→live` once it observes `ss == active`. This is race-free and
needs nothing from the box. The 11:45→12:00 operational warm-up (≥ one poll
interval) guarantees the broadcast is live before the consumer redirect opens
at 12:00.

## Box-side contract (dependency — separate plan)

This spec covers the monitor. It depends on the box providing:

1. **Window-gated playout** to the **same** operational window the monitor
   uses (start ~11:45, stop ~18:05 PT), so the stream goes `inactive→active` at
   open and `active→inactive` at close each day — a clean daily cycle. (This is
   exactly how galton-stream's `start.sh` behaved.)
2. **Self-heal** via the existing `playout.sh` loop (ffmpeg restarts on exit).
3. **`/health` endpoint** reachable over the public internet, gated by
   `BOX_HEALTH_TOKEN`, returning `playout_alive` / `ffmpeg_alive` /
   `heartbeat_age_s`.

Box-side systemd timer/service for window-gated playout and `/health` auth
hardening are tracked as a separate implementation plan. The shared window
definition must have a single source of truth (same env values on both sides).

## Removed from galton

`chat_poller`, `title_writer`, and all Railway-restart machinery
(`restart_ffmpeg`/`restart_railway`) — no Railway video service exists to
restart, and titles are baked into the show.

## Error handling

- API/transport failures: logged, return `None`, never crash the loop; alert on
  sustained failure.
- Token refresh failure: alert.
- Box unreachable: diagnostic only; does not by itself change lifecycle.
- Every reconcile action is idempotent and edge-triggered (guarded by a
  current-state check), so repeated polls converge without duplicate effects.

## Testing

- **`windows.py`** — pure unit tests across boundary minutes and DST.
- **`youtube.py`** — against a mocked API layer (reuse/extend
  `scripts/mock_youtube.py`). **Headline regression guard:** given a mix of
  owned and foreign broadcasts, assert `delete_stale_owned`, `go_live`, and
  `end_broadcast` touch **only** owned ones.
- **`redirect.py`** — mock boto3 S3/CloudFront; assert routing-rule shape and
  302 code.
- **reconcile** — table-driven over (window × stream status × owned/foreign
  broadcasts) → expected actions. Must include the case **out-of-window + a
  foreign `live` broadcast ⇒ no-op** (the bug that motivated this rewrite).

## Config / deployment

- Reuse the `galton-monitor` Railway service (service id
  `7cf592be-134d-47e9-90eb-e9f73b2dcdf6`, project `radio-dangerthirdrail`,
  env `production`); redeploy with the new code. Rename to `vxstory-monitor`
  optional.
- Environment:
  - `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`
  - `YOUTUBE_STREAM_KEY` — the **owned production key**. Recommend a dedicated
    persistent key; the `d70t` "Alt" key stays for ad-hoc tests.
  - `BOX_HEALTH_URL`, `BOX_HEALTH_TOKEN`
  - `RADIO_BUCKET`, `RADIO_CF_DISTRIBUTION_ID`, `RADIO_OFFLINE_HTML_PATH`
  - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
  - window config (operational/consumer start/end, TZ), `POLL_INTERVAL`

## Decisions locked

- Control model: observe + YouTube lifecycle (no inbound control into the box).
- Failure handling: ride it out (keep the broadcast across drops; redirect to
  the offline card; complete only at window close).
- Go-live: monitor-driven transition; `enableAutoStart` dropped.
- Windows: unchanged 6 hr/day (operational 11:45–18:05, consumer 12:00–18:00 PT).
