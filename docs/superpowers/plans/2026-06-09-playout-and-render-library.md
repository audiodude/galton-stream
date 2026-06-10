# Playout + Render Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** (1) A library of composable render helper scripts in this repo that turn the
vxstory checkout into a 720p60 mp4 catalog + manifest; (2) a playout stack (video pipe
mirroring the music pipe) with transitions at song boundaries and ident insertion;
(3) tonight: a local end-to-end test with a small catalog.

**Architecture (agreed in discussion, 2026-06-09):**
- Two repos, three contracts: S3 catalog+manifest, `/tmp` overlay text files, X11/RTMP
  source boundary. This repo owns operations including render orchestration; vxstory
  owns models. Renders run on the desktop GPU (cloud later, see TODO).
- 720p60 via a temporary `override.cfg` in the model project dir (verified: 66% of
  realtime on the 3080 Ti; `--resolution` does NOT change Movie Maker capture size).
- Playout mirrors the music architecture: `video_player.py` decodes the current piece
  to raw frames in a named pipe; ffmpeg muxes video pipe + audio pipe + drawtext title
  overlay → RTMP (or a local file in test mode). At a song boundary (detected from
  `/tmp/current_song.txt` changing) after a dwell threshold, the controller cuts:
  ident → next piece. Underrun policy: pieces render with headroom and are cut at the
  first song boundary after `DWELL_SEC`; they need not play to completion.
- Manifest `catalog.json`: `{"pieces": [{"id", "kind": "piece"|"ident", "model",
  "preset", "seed", "duration_sec", "file", "rendered_at"}]}`.

**Env:** `VXSTORY_DIR` (default `../vxstory`), `CATALOG_DIR` (default `/data/catalog`
on Railway, `./catalog` locally — gitignored).

**Branch:** work on `main`. NEVER touch `release` (auto-deploys production).

---

### Task P1: Render helper library + tonight's test catalog

**Files:** Create `scripts/render/render_piece.sh`, `scripts/render/make_manifest.py`,
`scripts/render/render_catalog.sh`, `scripts/render/upload_catalog.sh`; append
`catalog/` to `.gitignore`.

- [ ] `render_piece.sh <model> <preset> <seed> <duration_sec> <out_dir>` — composable
  unit. Copies the preset to a temp file with the seed substituted (jq), writes
  `override.cfg` (1280x720) into `$VXSTORY_DIR/<model>/`, runs
  `xvfb-run -a -s "-screen 0 1280x720x24" godot --path $VXSTORY_DIR/<model>
  --write-movie <tmp>.avi --fixed-fps 60 -- --preset <tmp preset> --duration <secs>`,
  ffmpeg-encodes to `<out_dir>/<model>_<preset>_s<seed>.mp4` (libx264 crf 18 yuv420p,
  60 fps), removes override.cfg + avi (trap-protected so override.cfg never lingers).
  NOTE: serialize renders (override.cfg is per-project state — no parallel renders of
  the same model).
- [ ] `make_manifest.py <out_dir>` — scans mp4s + a sidecar `.json` per piece written
  by render_piece.sh (id, kind, model, preset, seed, duration via ffprobe), emits
  `catalog.json`. Idents are any piece rendered with `--kind ident` flag.
- [ ] `render_catalog.sh <spec.json> <out_dir>` — loops a spec list of
  {model, preset, seed, duration_sec, kind} through render_piece.sh then
  make_manifest.py. Specs are committable (e.g. `scripts/render/specs/test-night.json`).
- [ ] `upload_catalog.sh <out_dir> <s3_uri>` — aws s3 sync + manifest last.
- [ ] Write `scripts/render/specs/test-night.json`: 4 short pieces + 1 ident:
  supernova_orbit/odyssey s101 240s; fluid_swirl/maelstrom s102 240s;
  matter_cycle/grinder s103 240s; chromatic_cascade/paintstorm s104 240s;
  radial_burst/cataclysm s105 12s kind=ident.
- [ ] Run it: `VXSTORY_DIR=../vxstory scripts/render/render_catalog.sh
  scripts/render/specs/test-night.json ./catalog` (~12 min GPU). Verify: 5 mp4s,
  1280x720, 60fps (ffprobe), catalog.json lists all with correct kinds.
- [ ] Commit (scripts + spec only; catalog/ is gitignored).

### Task P2: Playout stack

**Files:** Create `scripts/playout/video_player.py`, `scripts/playout/playout.sh`.

- [ ] `video_player.py` — reads `catalog.json`; maintains shuffled piece order
  (idents separate); decodes current piece via ffmpeg subprocess to rawvideo
  (yuv420p 1280x720@60) written to `$VIDEO_PIPE` (default `/tmp/video_pipe`);
  watches `$SONG_FILE` (default `/tmp/current_song.txt`) mtime+content each second;
  on song change with `elapsed >= DWELL_SEC` (env, default 900; test uses 90):
  kill decoder → play an ident fully → start next piece. If a piece's file ends
  before any boundary, chain straight into the next piece (no ident) — dead pipe is
  the only sin. Log transitions with timestamps to stdout.
- [ ] `playout.sh` — test-mode orchestrator (local): assumes music stack is already
  running (audio pipe + current_song.txt); starts video_player.py; runs master
  ffmpeg: video pipe (rawvideo) + audio pipe (s16le 44.1k stereo) + drawtext
  (textfile=/tmp/current_song.txt, reload=1, bottom-left, white w/ shadow) →
  `OUTPUT` env: default `./playout_test.flv` (file), or `rtmp://...` when
  `YOUTUBE_STREAM_KEY` is set (same x264 settings as start.sh: veryfast, 4.5Mbps
  region — copy the existing flags). 720p60 output, no scaling needed.
- [ ] Verify pipes don't deadlock on startup order (ffmpeg blocks until both pipes
  have writers — start video_player and music player first, ffmpeg last).
- [ ] Commit.

### Task P3: Tonight's local end-to-end test

- [ ] Music stack locally: sync a handful of MP3s from S3 (creds in `~/.secrets`)
  to `./music_local` (gitignored), run `music_player.py` + `title_writer.py` with
  env overrides for paths. If S3 sync is blocked for any reason, fall back to any
  local MP3s — the test needs real song boundaries, not specific songs.
- [ ] Run playout with `DWELL_SEC=90`, `OUTPUT=./playout_test.flv` for ~10 minutes.
- [ ] Verify from logs + probing the flv: transitions occurred at song-change
  timestamps (compare video_player log to song log), ident played between pieces,
  title overlay visible, 720p60 stream, A/V in sync (±100 ms). The user reviews the
  flv for aesthetics — do NOT frame-inspect beyond basic correctness probes.
- [ ] Hand off: file ready for user review; YouTube RTMP test is a separate user
  decision (needs a stream key — manual step, ask).

---

## Out of scope tonight
Railway service, S3 catalog upload (script exists, not exercised), Galton rotation,
crossfades (hard cuts only), the 50-piece catalog (needs model tuning passes first).
