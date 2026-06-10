# TODO — station + content roadmap

(Migrated from vxstory/TODO.md 2026-06-09 and expanded; this is the single roadmap
for the radio + vxstory system. vxstory repo: https://github.com/audiodude/vxstory)

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
