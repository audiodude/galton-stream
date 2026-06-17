# Monitor / Broadcast Coordination — Findings & Recommended Fixes

**Date:** 2026-06-17
**Status:** Recommendations (not yet implemented). `monitor.py` deploys via `release`
— do not implement without an explicit deploy decision.

## What happened

Every attempt to run the vxstory pre-baked playout live on the shared YouTube channel
died after 22–105 s, across weeks, with a red-herring "Video output low" warning. The
pre-baked pipeline itself is sound (box bakes copy-safe CBR; YouTube ingest `health=good`).
The deaths were **galton-monitor reaping the broadcasts** — its own logs:

```
[monitor] Transitioned broadcast -WS4sh31__I -> complete   ← vxstory CBR test
[monitor] Transitioned broadcast o9IGHHnbBFQ -> complete
[monitor] Transitioned broadcast RSvajz5upI4 -> complete
[monitor] Broadcast ended: ...
```

## Root cause (precise)

`reconcile_broadcast()` (monitor.py ~556–648) runs every poll, 24/7. Its **out-of-window
teardown** acts on `get_live_or_pending_broadcasts()` **unfiltered** — i.e. every live
broadcast on the channel:

```python
# Outside operational window:
running = [b for b in actives
           if b.get("status", {}).get("lifeCycleStatus") in ("live", "testing")]
for b in running:
    transition_broadcast(bid, "complete")      # <-- completes ANY live broadcast
    set_broadcast_privacy(bid, "private")
```

The asymmetry is the bug: the **in-window** paths already scope to galton's stream
(`get_production_stream_id()` — see the stale-delete loop at ~615 and the create+bind),
but the **out-of-window teardown** and the **in-window live-detection** (`live_broadcast`,
~573) consider *all* broadcasts. So a second producer on the same channel (vxstory
playout, bound to the `d70t` "Alt" stream) is treated as galton's and reaped — and
in-window would be mistaken for galton's live show and drive the radio redirect.

The out-of-window block's own comment already shows the right instinct for *upcoming*
broadcasts ("Scheduled-but-never-started … are the user's own, leave them alone") — it
just isn't applied to `live`/`testing`.

## Fix A — scope all reconcile actions to the production stream (minimal, recommended)

Only ever touch broadcasts bound to galton's stream. One ownership predicate, applied in
three places:

```python
def _owned(b, stream_id):
    return b.get("contentDetails", {}).get("boundStreamId") == stream_id
```

1. **Out-of-window teardown (~636):** filter `running` to `_owned(b, get_production_stream_id())`.
   Reap only galton's live broadcast; leave others (vxstory) alone.
2. **In-window live detection (~573):** `live_broadcast = next(b for b in actives if
   live/testing and _owned(b, stream_id))` — so the radio redirect tracks galton's
   broadcast, not whatever else is live.
3. **`get_live_or_pending_broadcasts`** already returns all; the scoping lives at the
   call sites (it's also used for the in-window stale-delete, which is already scoped).

Cost: ~10 lines, no behavior change for galton (it only ever owned its own stream's
broadcasts). Effect: the channel becomes safely shareable — galton-monitor never touches
a broadcast it didn't create. **This is correct regardless of the production channel
decision and should land before any shared-channel use.**

## Fix B — defense in depth (optional)

Tag galton broadcasts (a marker in description, e.g. `mgr=galton`) and/or keep an
owned-broadcast-id set persisted across restarts; never transition a broadcast lacking
the marker. Protects against stream-binding ambiguity (e.g. if a broadcast's
`boundStreamId` is briefly absent during transitions). Heavier; only if Fix A proves
insufficient.

## Production architecture (the deferred decision this forced)

vxstory playout cannot share a channel with an unscoped reaper. With **Fix A**, three
viable shapes:

- **Shared channel, two scoped monitors.** galton-monitor (Fix A) ignores vxstory;
  vxstory playout runs its own monitor scoped to its stream + its own window. Works, but
  two services drive one channel's "what's live now" + the radio redirect — they must
  not both be the public-live broadcast simultaneously (alternate windows, or one owns
  the redirect). Coordination is light but real.
- **Separate channel for vxstory.** Its own channel + monitor; zero scoping needed,
  total isolation. Cost: a second channel to grow/manage; the redirect points at
  whichever property is "on."
- **Unified scheduler.** One monitor schedules galton + vxstory as sources into a single
  channel's broadcast rotation. Most integrated, biggest change; the natural end state if
  the station becomes "one channel, many shows."

**Recommendation:** Land **Fix A** unconditionally (it's a latent correctness bug even
without vxstory — any stray live broadcast on the channel gets reaped today). Pick the
channel strategy when productionizing vxstory playout; the **separate channel** is the
lowest-coordination start, **unified scheduler** the eventual goal.

## Stopgap used for the 2026-06-17 test

galton-monitor was paused (Railway service stopped) for the ~45-min live test, then
restarted — no code change. Fix A is the durable replacement for that stopgap.
