# TODO — station + content roadmap

(Migrated from vxstory/TODO.md 2026-06-09 and expanded; this is the single roadmap
for the radio + vxstory system. vxstory repo: https://github.com/audiodude/vxstory)

## Known seams (open edges of the playout architecture, 2026-06-10)

The local end-to-end tests work; these are the boundaries that will need attention
as the system goes to production, in the order they'll likely bite:

1. **Catalog freshness/distribution** — playout reads a local dir; the Railway
   service needs the music-style "sync from S3 at window open" plus a policy for
   picking up nightly catalog additions (full re-sync per day is probably enough;
   manifest-uploaded-last already guarantees consistency mid-sync).
2. **Scheduling intelligence lives nowhere** — shuffle-with-no-repeat in
   `video_player.py` is the entire brain. Mood/time-of-day curation, ident variety,
   and "don't repeat within N hours" all want manifest fields (tags) + selection
   logic in the video player. Design the manifest fields before the 50-piece
   catalog render so pieces are born tagged.
3. **Cut latency** (~5–6 s after the audible song change) — cost of 1 Hz file-watch
   as the boundary signal. Acceptable now. If it grates: music_player knows track
   durations, so it could write a *predicted* boundary timestamp; video_player
   could then cut frame-exact (or even pre-roll the ident).
4. **Render economics** — xvfb renders run ~3× realtime (vs ~1.5× on the live X
   display; root cause unexamined — readback stalls?). 50×20 min ≈ 50 GPU-hours
   per full catalog. Feeds the cloud-rendering item below; also worth one session
   profiling WHY xvfb is slower.

### Video pacer thread (playout) — first task of next playout session
video_player.py: a single wall-clock pacer thread owns all pipe writes at exactly
60fps from a latest-frame slot; decoders feed the slot. Makes video supply
structurally constant through transitions/warmups/EOF (subsumes the held-frame
bridge), ending the videoIngestionStarved dips that flap YouTube's health grade
when transitions are frequent. Verified need: 2026-06-11 live session — supply
dips ~0.2-0.4s per transition survive all micro-optimizations. PLUS: -re decoder
pacing runs slightly sub-realtime, draining YouTube's player buffer (~30-40s to
empty) — the all-night "dies after 30s" symptom. Production precedent: galton's
x11grab input IS a wall-clock pacer (samples the display at exactly 30fps
regardless of render state) — the pacer thread restores that invariant for
file-decoder playout. Audio side already has production parity (-re + queue 8 on
the pipe input; see queue_blocking_report.md for why big queues hide drift).

## Infrastructure

### Cloud-compute rendering
Move catalog rendering off the desktop GPU to cloud machines (GPU instances, spot
pricing). The render helpers in `scripts/render/` are the seam: they already take a
vxstory checkout + preset + seed and emit mp4s + manifest entries, so "the cloud
version" is the same scripts on a rented box + S3 upload. Needs: image with Godot
4.6 + Xvfb + NVIDIA driver, cost math vs. nightly 3080 Ti batches.

### Railway playout service
Promote the local playout test rig into a `radio-playout` Railway service alongside
galton-stream: ffmpeg muxing the S3 video catalog (video pipe) + music (audio pipe),
song-boundary transitions, title overlay. Then decide Galton's place in the rotation.

## Content (vxstory)

### Long-form pass for the remaining five models
supernova_orbit got the treatment (director wiring to visually potent params,
persistence); radial_burst, fluid_swirl, peg_cascade, chromatic_cascade, matter_cycle
still need theirs before the 50-piece catalog is rendered.

### Station idents — new lightweight model genre
5–15 s whacky pieces played at song-boundary transitions. Separate small 2D AND 3D
models (3D returns: lattice ideas etc.), one-gag designs that would never sustain
20 minutes. Low-stakes sandbox for new-model R&D. Manifest `kind: ident`.

### Epochs — turn the supernova loop into a life story
Nebula → protostar/disk → supernova → remnant path (pulsar beams / black-hole
inverted infall) → wreckage seeds next nebula. ~5 epochs × ~60 s, a full non-looping
arc. Builds on the binary-core state machine.

### Heavy-tailed rare events (all models)
Seeded low-probability spectacles (rogue mass flyby slingshotting the disk, double
detonations, golden ball in peg models). Rarity creates anticipation; same seeded
RNG streams so renders stay reproducible.

### Camera breathing (framework-level)
Director-driven Camera2D: slow drift/zoom in calm phases, punch-in on climaxes,
pull-back to reveal accumulated scale. Models publish "interest points."

## Interactivity (deferred by design)

### Async chat reactivity
Today's chat influences tomorrow's renders: gift totals set energy macros, top
chatter's name seeds a piece. Zero runtime risk, pre-rendered quality retained.

### Live reactive segment
A broadcast-budget live model (compatibility renderer, small particle counts) as one
rotation slot — e.g. peg cascade firing named balls for chat gifts. The Galton board
already proves the live-source pattern.
