# Pre-baked Playout — Design Spec

**Date:** 2026-06-16
**Status:** Approved direction, pending implementation plan
**Repo:** radio.dangerthirdrail.com (vxstory catalog is the upstream content source)

## Overview

Stream the daily generative-video + music program to YouTube by **pre-baking the
entire show offline** into one muxed file, then **stream-copying it live**. Video
cuts land exactly on song boundaries because the cuts are baked at frame-accurate
positions offline, where precision is free. The live path is the most bulletproof
ffmpeg invocation that exists — read a local file, pace to realtime, copy to RTMP —
so it cannot underrun, cannot desync, and needs no realtime encode.

This replaces the live dynamic-switching playout (`scripts/playout/`,
`video_player.py` and its pacer/pipe/frame-alignment machinery), which fought
underruns and clock conflicts and still died ~22s into a live broadcast. Those
problems are *defined out of existence* here rather than managed.

### Why this and not the alternatives

- **Live dynamic switching (the failed approach):** cutting at runtime-known song
  boundaries forces source-switching → swap gaps → holdover buffers → multiple
  wall-clock pacers fighting one mux. Fragile.
- **x11grab off a display:** correct for galton-stream (a *live* Godot app whose
  only output is a rendered display), but wasteful for files — decode → blit to a
  screen → screen-grab → re-encode is the screenshot-and-OCR anti-pattern.
- **Pre-bake (this spec):** the song order is deterministic once chosen, so every
  boundary timestamp is known offline. Compute the whole timeline, composite it
  once at high quality, stream-copy it live.

### Inherited lessons (from the solved galton-stream pipeline)

- One realtime clock owns the mux. Here it is `-re` on a single file input.
- Offline encode can use a slow preset / higher bitrate (no realtime constraint),
  so quality goes *up* vs. the live `ultrafast` encoder.
- The YouTube **broadcast lifecycle** (create / bind / bounce-after-bind /
  auto-start / keep-alive / complete / health / recovery / redirect) is already
  solved in `monitor/monitor.py` and is orthogonal to how the bits are produced.
  Reuse it unchanged.

## Goals & Success Criteria

1. **Frame-exact song-boundary cuts.** Each baked video cut/ident lands within ±1
   frame of its computed song boundary (verifiable: probe segment timecodes vs.
   the plan).
2. **Bulletproof live path.** `ffmpeg -re -i show.mkv -c copy -f flv …` sustains
   the full operational window with healthy YouTube ingest — no "Video output
   low", no early `complete`. Live CPU is network-bound (no encode).
3. **Titles track songs exactly.** The on-screen song title changes at each song
   boundary, frame-accurate, burned in.
4. **Reproducible.** Same seeds + same catalog + same library → byte-identical
   plan manifest (and a re-bake reproduces an equivalent show).
5. **Quality.** 720p60, offline-encoded at a quality the live encoder couldn't
   afford.
6. **Survivable.** A missing/invalid daily bake falls back to a prior valid show
   rather than dead air.

## Architecture

Three clean layers — brain (pure logic), muscle (offline ffmpeg), live (copy):

```
                         ┌──────────── BAKE (offline, ahead of window) ────────────┐
 vxstory catalog ─┐      │                                                          │
 (pieces+idents,  ├────▶ │  1. plan.py    → show.plan.json  (music timeline,        │
  catalog.json)   │      │     (pure, fast)   video EDL, subtitle cues)             │
 music library ───┘      │                                                          │
 (S3 mp3s, trimmed)      │  2. assemble   → show.mkv  (concat EDL + burn titles +   │
 config (seeds,DWELL,    │     (ffmpeg)       HQ encode; concat audio; mux)         │
  res, bitrate, dur)     │                                                          │
                         │  3. validate   → mark show.mkv valid (ffprobe vs plan)   │
                         └──────────────────────────┬───────────────────────────────┘
                                                     │  show-YYYY-MM-DD.mkv (+ .plan.json)
                         ┌──────────── LIVE (operational window) ───────────────────┐
                         │  4. playout.sh:  ffmpeg -re -i show.mkv -c copy -f flv     │
                         │     (retry loop, /health, fallback to prior show)         │
                         │  5. monitor.py (existing): broadcast lifecycle + redirect │
                         │     + health + recovery — UNCHANGED                       │
                         └──────────────────────────────────────────────────────────┘
```

### The spine: one deterministic music timeline feeds everything

The single source of truth is the **music timeline** computed in `plan.py`. From the
ordered playlist and each track's exact duration (ffprobe), it derives cumulative
timecodes — and *three* outputs hang off that one computation:

- **Audio track** — the songs concatenated in that order.
- **Video EDL cuts** — video transitions fire at a *subset* of song boundaries.
- **Title cues** — a subtitle entry per song at its exact `[start, end)`.

Because all three derive from the same timecodes, the cuts, the audio, and the
titles are mutually frame-consistent by construction — no live sync, no drift.

Note the two independent cadences: **songs change often** (~3–4 min), **video
changes less often** (governed by DWELL). A single video segment may span several
songs, so titles are *not* per-segment — they are an independent subtitle track over
the whole timeline.

### Component 1 — `plan.py` (the brain; pure, deterministic, no media writes)

**Inputs:** vxstory `catalog.json` (pieces + idents with durations), the music
library (paths; durations via ffprobe, cached), and config:
`{seed, dwell_sec, show_dur_sec, fps, resolution, ident_straddle}`.

**Algorithm:**

1. **Music timeline.** Seeded-shuffle the library; append tracks until cumulative
   duration ≥ `show_dur_sec`. Record each track's `{file, title, start, end}`.
   Boundaries `B_i = start_i` (i ≥ 1) are the candidate video cut points.
2. **Video EDL.** Seeded-shuffle the pieces (no immediate repeat, cycle as needed).
   Walk the timeline:
   ```
   t = 0
   while t < show_dur:
       piece      = next_piece()
       cut_at     = first boundary B_i with B_i >= t + dwell_sec   (else show_dur)
       ident      = next_ident()
       # ident straddles the boundary (preserves the established aesthetic)
       ident_in   = cut_at - ident.dur/2
       ident_out  = cut_at + ident.dur/2
       emit piece segment: src=piece, in=0, out=(ident_in - t), tl_start=t
       emit ident segment: src=ident, in=0, out=ident.dur, tl_start=ident_in
       t = ident_out
   clamp the final segment to show_dur
   ```
   **Constraint:** `dwell_sec + max_song_dur ≤ piece_source_dur` so a segment never
   outruns its source (default DWELL 600s, 20-min sources, ≤6-min songs → ≤16 min
   segment < 20 min). `plan.py` asserts this and fails loudly otherwise.
3. **Subtitle cues.** One ASS cue per song: title text over `[start, end)`, with the
   existing fade aesthetic (full opacity briefly, then dim) expressed as ASS styling.

**Output:** `show.plan.json`:
```json
{
  "date": "2026-06-16", "seed": 12345, "fps": 60, "resolution": "1280x720",
  "show_dur_sec": 22800.0,
  "music": [{"file":"…/drip.mp3","title":"Drip","start":0.0,"end":182.4}, …],
  "edl":   [{"kind":"piece","src":"…/supernova_orbit_odyssey.mp4","in":0.0,"out":612.0,"tl_start":0.0},
            {"kind":"ident","src":"…/text_ident_plz_ignore.mp4","in":0.0,"out":12.0,"tl_start":612.0}, …],
  "subtitles": [{"text":"Drip","start":0.0,"end":182.4}, …],
  "catalog_sha": "…", "library_sha": "…"
}
```
Pure and fast → unit-testable without touching ffmpeg (assert boundary math, the
DWELL constraint, no-repeat, total duration, determinism).

### Component 2 — `assemble` (the muscle; offline ffmpeg, HQ)

Consumes `show.plan.json` + source media, produces `show-DATE.mkv`.

- **Video:** realize the EDL (trim each piece/ident slice), concatenate, burn the
  ASS subtitle titles, and encode **once** at high quality — `libx264 -preset slow`
  (or NVENC `hevc`/`h264_nvenc` for speed), `-pix_fmt yuv420p`, **closed GOP,
  keyframe every 2 s** (`-g 2*fps -keyint_min 2*fps -sc_threshold 0`), target
  bitrate above what the live encoder could afford (e.g. ~6 Mbps / `-maxrate
  6M -bufsize 12M`, or 2-pass). The 2 s closed-GOP cadence is what makes the live
  `-c copy` YouTube-valid.
- **Audio:** concat the playlist tracks → AAC 128k 44.1k stereo (apply the existing
  `-7 dB` normalization).
- **Mux:** one CFR `show.mkv`, video + audio, timestamps from zero.

**Implementation note (to settle in the plan):** two viable topologies — (a) one
`filter_complex` (concat + subtitles) pass, or (b) render trimmed segments → concat
→ one subtitle+encode pass. Favor (b): composable, parallelizable, cacheable
(unchanged idents/piece-slices needn't re-render), and aligns with future cloud
bake. Either way it is **one video encode pass total**.

### Component 3 — `validate`

ffprobe `show.mkv` against the plan: duration within tolerance, exactly one H.264
video + one AAC audio stream, resolution/fps correct, keyframe interval ≈ 2 s, and
spot-check that segment-boundary timecodes match the EDL within ±1 frame. Only a
passing bake is marked the day's valid show (atomic rename / a `LATEST` pointer).

### Component 4 — `playout.sh` (live; trivial)

```bash
ffmpeg -re -i "$SHOW" -c copy -f flv "$YOUTUBE_URL/$YOUTUBE_STREAM_KEY"
```
Wrapped in the existing retry-on-exit loop. Picks the day's valid `show.mkv`; if
absent/invalid, falls back to the most recent valid show (never dead air). Exposes
`/health` exactly like galton-stream so `monitor.py` can poll it. No Xvfb, no
encode, no pipes, no music_player/title_writer/video_player at stream time.

### Component 5 — `monitor.py` (live; unchanged)

Reuse as-is for broadcast create/bind/**bounce-after-bind**/auto-start/keep-alive/
complete, health escalation, and the radio redirect. Pre-bake removes the
*stream-health* cause of the 22 s death (a clean copy ingests healthy); the
lifecycle plumbing the monitor already handles is orthogonal and stays.

## Scheduling (6 h/day)

Bake **ahead of** the operational window (overnight cron, or T-minus-hours trigger);
at window open, `playout.sh` streams the prepared file and `monitor.py` brings the
broadcast live. `show_dur_sec` = operational-window length (≈ 11:45–18:05 PT =
6 h 20 m) so warmup→cooldown is covered. With a full pre-bake there is **no
head-start buffer needed** — the entire show is on disk before streaming begins. (A
just-in-time variant — start baking at T-30, stream once buffered — is a valid
fallback if bake ever runs same-day; a 10–30 min head start covers bake-vs-realtime
jitter. Not the default.)

## Tradeoffs & Notes

- **The show is fixed once baked** — no live reactivity. Acceptable (generative
  video + a fixed music library; "live" was never the point). The deferred
  *async chat-reactivity* idea — today's chat shapes tomorrow's render — is
  *naturally* a pre-bake input, so this architecture is on the path there, not
  against it.
- **Titles burned at bake** force the one video encode pass (drawtext/subtitles
  can't stream-copy). Paid once, offline, at high quality — and it buys the
  zero-encode live path.
- **Re-bake to change content** (nightly anyway for fresh catalog). Reusable
  segments (topology b) make partial re-bakes cheap later.
- **Single-artifact fragility** — one bad file kills the day. Mitigated by
  `validate` + the prior-show fallback.
- **Quality/CPU both improve** vs. live encoding; **no X server / no double
  colorspace conversion** (directly answers the resource-waste objection).

## Risks

- **Bake time vs. window.** A slow-preset 6 h 720p60 encode may run near or below
  realtime on CPU. Mitigations: overnight bake (we have all night), NVENC, or the
  JIT+head-start variant. The plan should measure actual bake throughput early.
- **assemble topology complexity** (many segments). Mitigate by prototyping the
  segment-then-concat path and validating concat-copy seams (closed GOP, identical
  codec params).
- **Storage.** ~5–10 GB per daily show; rotate/retain a few days for fallback.

## Out of Scope

- Live chat reactivity (deferred; fits pre-bake later).
- Whether vxstory playout replaces or rotates with the live galton board (separate
  integration decision; this spec delivers a streamable program either way).
- Cloud bake (the render TODO already tracks moving heavy encode off the desktop).
- Deleting the old live-playout code — sequenced in the implementation plan once
  pre-bake is proven, not before.
