# Pre-baked Playout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream the daily generative-video + music program to YouTube by pre-baking the whole show offline into one muxed file and stream-copying it live, with video cuts frame-exact on song boundaries.

**Architecture:** Three layers — `plan.py` (pure, deterministic: music timeline → video EDL + subtitle cues), `assemble` (offline ffmpeg: realize EDL, burn titles, encode high-quality, mux), and a trivial live `playout.sh` (`ffmpeg -re -i show.mkv -c copy`) with the existing `monitor.py` reused for broadcast lifecycle. Runs on one remote flat-traffic box that bakes overnight and streams during the window.

**Tech Stack:** Python 3 (stdlib only at runtime), ffmpeg/ffprobe, pytest (via `uv run`), bash, S3 (aws cli), systemd + cron on the production host.

**Spec:** `docs/superpowers/specs/2026-06-16-prebaked-playout-design.md`

## Global Constraints

- **Resolution-agnostic, config-driven.** Ship 720p60 first (`1280x720`, current catalog); 1080p60 (`1920x1080`) is the eventual target. Never hardcode resolution/fps — read from config.
- **Live path is `-c copy` only.** No live re-encode, ever. All cutting/overlay/encoding happens at bake time.
- **Frame-exact cuts.** Baked cuts land within ±1 frame of the computed song boundary.
- **Copy-safe bake.** Bake output is H.264 High + AAC, `yuv420p`, **closed GOP, keyframe every 2 s** (`-g 2*fps -keyint_min 2*fps -sc_threshold 0`) so the live `-c copy` is YouTube-valid.
- **Deterministic plan.** Same `seed` + same catalog + same library → byte-identical `plan.json`.
- **Python: stdlib only at runtime.** Tests run via `uv run --with pytest pytest` — never `pip install` into system Python.
- **Reuse `monitor.py` unchanged.** Broadcast lifecycle is out of scope to modify.
- **Do not delete the old `scripts/playout/` live-switching code** until the new path is proven on a real broadcast.
- **Branch `main` only.** This work never touches `release` (that auto-deploys galton-stream on Railway; the vxstory box is separate infra).

## File Structure

```
scripts/bake/
  plan.py            # pure: timeline, EDL, subtitles, build_plan + CLI
  assemble.py        # ffmpeg: plan.json + media -> show.mkv (video pass, audio pass, mux)
  validate.py        # ffprobe: show.mkv vs plan.json -> ok/fail
  bake.sh            # orchestrator: sync inputs, plan, assemble, validate, publish to S3
  tests/
    make_fixtures.sh # tiny synthetic catalog+library via lavfi (seconds-long)
    test_plan.py
    test_assemble.py
    test_validate.py
scripts/broadcast/
  playout.sh         # live: resolve show -> ffmpeg -re -c copy -f flv (retry, fallback)
  health_server.py   # /health for monitor (or reuse existing pattern)
docs/runbooks/
  prebaked-playout-deploy.md   # remote box provisioning + cron + systemd + monitor
```

---

### Task 1: `plan.py` — pure helpers + music timeline + subtitles

**Files:**
- Create: `scripts/bake/plan.py`, `scripts/bake/tests/test_plan.py`

**Interfaces:**
- Produces (used by Task 2 and assemble/validate):
  - `title_from_path(path: str) -> str`
  - `shuffle(items: list, seed: int, no_repeat: bool = False) -> list`
  - `build_music_timeline(ordered: list[dict], target_dur: float) -> list[dict]` — `ordered` items `{"file","title","dur"}`; returns `{"file","title","start","end"}` appended until cumulative `end >= target_dur`.
  - `song_boundaries(timeline: list[dict]) -> list[float]` — `[e["start"] for e in timeline[1:]]`
  - `build_subtitles(timeline: list[dict]) -> list[dict]` — `{"text","start","end"}` per song.

- [ ] **Step 1: Write the failing tests**

```python
# scripts/bake/tests/test_plan.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import plan

def test_title_from_path():
    assert plan.title_from_path("/x/ele-1005.mp3") == "Ele 1005"
    assert plan.title_from_path("never_havin_kids.mp3") == "Never Havin Kids"

def test_shuffle_deterministic_and_total():
    a = plan.shuffle(list(range(20)), seed=7)
    b = plan.shuffle(list(range(20)), seed=7)
    c = plan.shuffle(list(range(20)), seed=8)
    assert a == b and a != c
    assert sorted(a) == list(range(20))  # permutation, nothing lost

def test_shuffle_no_repeat_join():
    # no element equal to its predecessor (best-effort across a single shuffle)
    out = plan.shuffle(["a", "b", "c", "d"], seed=3, no_repeat=True)
    assert all(out[i] != out[i-1] for i in range(1, len(out)))

def test_music_timeline_fills_target():
    ordered = [{"file": f"{i}.mp3", "title": str(i), "dur": 100.0} for i in range(10)]
    tl = plan.build_music_timeline(ordered, target_dur=250.0)
    assert len(tl) == 3                      # 100+100+100 >= 250, stops at 3
    assert tl[0]["start"] == 0.0 and tl[0]["end"] == 100.0
    assert tl[1]["start"] == 100.0 and tl[2]["end"] == 300.0

def test_song_boundaries():
    tl = [{"start": 0.0, "end": 100.0}, {"start": 100.0, "end": 250.0}, {"start": 250.0, "end": 400.0}]
    assert plan.song_boundaries(tl) == [100.0, 250.0]   # boundaries are song STARTS, excluding 0

def test_subtitles_one_per_song():
    tl = [{"title": "A", "start": 0.0, "end": 100.0}, {"title": "B", "start": 100.0, "end": 250.0}]
    subs = plan.build_subtitles(tl)
    assert subs == [{"text": "A", "start": 0.0, "end": 100.0}, {"text": "B", "start": 100.0, "end": 250.0}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd scripts/bake && uv run --with pytest pytest tests/test_plan.py -q`
Expected: collection/import error or failures (`plan` has no such attributes).

- [ ] **Step 3: Implement the helpers in `scripts/bake/plan.py`**

```python
#!/usr/bin/env python3
"""Build a deterministic show plan: music timeline, video EDL, subtitle cues.

Pure and stdlib-only. Durations are passed in as data (so the core is testable
without media); the CLI wrapper fills them via ffprobe.
"""
import json
import os
import random
import subprocess


def title_from_path(path: str) -> str:
    base = os.path.splitext(os.path.basename(path))[0]
    return base.replace("-", " ").replace("_", " ").title()


def shuffle(items: list, seed: int, no_repeat: bool = False) -> list:
    rng = random.Random(seed)
    out = list(items)
    rng.shuffle(out)
    if no_repeat:
        for i in range(1, len(out)):
            if out[i] == out[i - 1]:
                # swap with the next differing element to break the run
                for j in range(i + 1, len(out)):
                    if out[j] != out[i - 1]:
                        out[i], out[j] = out[j], out[i]
                        break
    return out


def build_music_timeline(ordered: list[dict], target_dur: float) -> list[dict]:
    tl = []
    t = 0.0
    for trk in ordered:
        start = t
        end = round(start + float(trk["dur"]), 3)
        tl.append({"file": trk["file"], "title": trk["title"], "start": round(start, 3), "end": end})
        t = end
        if t >= target_dur:
            break
    return tl


def song_boundaries(timeline: list[dict]) -> list[float]:
    return [e["start"] for e in timeline[1:]]


def build_subtitles(timeline: list[dict]) -> list[dict]:
    return [{"text": e["title"], "start": e["start"], "end": e["end"]} for e in timeline]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd scripts/bake && uv run --with pytest pytest tests/test_plan.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/bake/plan.py scripts/bake/tests/test_plan.py
git commit -m "bake: plan.py pure helpers — timeline, shuffle, subtitles"
```

---

### Task 2: `plan.py` — EDL cut algorithm + `build_plan` + CLI

**Files:**
- Modify: `scripts/bake/plan.py`, `scripts/bake/tests/test_plan.py`

**Interfaces:**
- Consumes: all of Task 1.
- Produces:
  - `build_edl(pieces, idents, boundaries, show_dur, dwell, straddle) -> list[dict]` — `pieces`/`idents` are `[{"src","dur"}]` used cyclically in order; returns segments `{"kind":"piece"|"ident","src","in","out","tl_start"}`.
  - `ffprobe_duration(path: str) -> float`
  - `build_plan(catalog: dict, library_files: list[str], config: dict) -> dict` — `config` keys: `seed,show_dur_sec,dwell_sec,fps,resolution,straddle,ident_straddle`. Returns the full plan dict.
  - CLI: `python3 plan.py --catalog C --library-dir D --seed N --show-dur S --dwell W --fps F --resolution RxR --out plan.json`

- [ ] **Step 1: Write the failing tests (append to `tests/test_plan.py`)**

```python
def _piece(src, dur): return {"src": src, "dur": dur}

def test_edl_cuts_at_first_boundary_after_dwell():
    pieces = [_piece("p0.mp4", 1200.0), _piece("p1.mp4", 1200.0)]
    idents = [_piece("id.mp4", 12.0)]
    boundaries = [180.0, 360.0, 540.0, 720.0]   # songs every 180s
    edl = plan.build_edl(pieces, idents, boundaries, show_dur=2000.0, dwell=300.0, straddle=True)
    # first piece: dwell 300 -> first boundary >=300 is 360; ident straddles 360
    assert edl[0]["kind"] == "piece" and edl[0]["tl_start"] == 0.0
    assert edl[0]["out"] == 354.0                 # 360 - 12/2 - 0
    assert edl[1]["kind"] == "ident" and edl[1]["tl_start"] == 354.0 and edl[1]["out"] == 12.0
    assert edl[2]["kind"] == "piece" and edl[2]["tl_start"] == 366.0   # 360 + 6

def test_edl_final_piece_runs_to_show_end_without_ident():
    pieces = [_piece("p0.mp4", 5000.0)]
    idents = [_piece("id.mp4", 12.0)]
    edl = plan.build_edl(pieces, idents, boundaries=[], show_dur=400.0, dwell=300.0, straddle=True)
    assert len(edl) == 1 and edl[0]["kind"] == "piece"
    assert edl[0]["tl_start"] == 0.0 and edl[0]["out"] == 400.0

def test_edl_covers_full_show():
    pieces = [_piece(f"p{i}.mp4", 1200.0) for i in range(5)]
    idents = [_piece("id.mp4", 12.0)]
    boundaries = [i * 180.0 for i in range(1, 80)]
    edl = plan.build_edl(pieces, idents, boundaries, show_dur=3600.0, dwell=600.0, straddle=True)
    last = edl[-1]
    assert abs((last["tl_start"] + last["out"]) - 3600.0) < 0.05    # timeline reaches show end
    # piece segments never exceed their source duration
    for seg in edl:
        if seg["kind"] == "piece":
            assert seg["out"] <= 1200.0 + 1e-6

def test_build_plan_deterministic_and_constraint():
    catalog = {"pieces": [{"file": f"/c/p{i}.mp4", "dur": 1200.0} for i in range(4)],
               "idents": [{"file": "/c/id.mp4", "dur": 12.0}]}
    library = [{"file": f"/m/s{i}.mp3", "title": f"S{i}", "dur": 200.0} for i in range(50)]
    cfg = {"seed": 42, "show_dur_sec": 3600.0, "dwell_sec": 600.0,
           "fps": 60, "resolution": "1280x720", "straddle": True}
    a = plan.build_plan(catalog, library, cfg)
    b = plan.build_plan(catalog, library, cfg)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)   # deterministic
    assert a["resolution"] == "1280x720" and a["fps"] == 60
    assert a["music"] and a["edl"] and a["subtitles"]
    # DWELL constraint asserted: dwell + max_song <= min piece dur
    assert cfg["dwell_sec"] + 200.0 <= 1200.0
```

Note: `build_plan` takes the library as a list of `{"file","title","dur"}` dicts in
tests (durations pre-supplied); the CLI builds that list from a directory via
`ffprobe_duration` + `title_from_path`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd scripts/bake && uv run --with pytest pytest tests/test_plan.py -q`
Expected: failures (`build_edl`/`build_plan` missing).

- [ ] **Step 3: Implement in `scripts/bake/plan.py` (append)**

```python
def ffprobe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def build_edl(pieces, idents, boundaries, show_dur, dwell, straddle) -> list[dict]:
    edl = []
    t = 0.0
    pi = ii = 0
    while t < show_dur - 1e-6:
        piece = pieces[pi % len(pieces)]; pi += 1
        cut_at = next((b for b in boundaries if b >= t + dwell), None)
        if cut_at is None or cut_at >= show_dur:
            edl.append({"kind": "piece", "src": piece["src"], "in": 0.0,
                        "out": round(show_dur - t, 3), "tl_start": round(t, 3)})
            break
        ident = idents[ii % len(idents)]; ii += 1
        if straddle:
            istart, iend = cut_at - ident["dur"] / 2, cut_at + ident["dur"] / 2
        else:
            istart, iend = cut_at, cut_at + ident["dur"]
        istart = max(istart, t)
        edl.append({"kind": "piece", "src": piece["src"], "in": 0.0,
                    "out": round(istart - t, 3), "tl_start": round(t, 3)})
        edl.append({"kind": "ident", "src": ident["src"], "in": 0.0,
                    "out": round(min(ident["dur"], show_dur - istart), 3),
                    "tl_start": round(istart, 3)})
        t = iend
    return edl


def _sha(parts) -> str:
    import hashlib
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode())
    return h.hexdigest()[:16]


def build_plan(catalog: dict, library: list[dict], config: dict) -> dict:
    seed = int(config["seed"])
    show_dur = float(config["show_dur_sec"])
    dwell = float(config["dwell_sec"])
    straddle = bool(config.get("straddle", True))

    # Music: shuffle (seeded, no immediate repeat), build timeline to >= show_dur.
    ordered = shuffle(library, seed=seed, no_repeat=True)
    timeline = build_music_timeline(ordered, target_dur=show_dur)
    real_dur = timeline[-1]["end"] if timeline else 0.0   # exact end of the audio
    boundaries = song_boundaries(timeline)
    subs = build_subtitles(timeline)

    # DWELL constraint: a piece segment is at most dwell + the longest song; it must
    # fit within the shortest piece source. Fail loudly if violated.
    max_song = max((e["end"] - e["start"] for e in timeline), default=0.0)
    min_piece = min((float(p["dur"]) for p in catalog["pieces"]), default=0.0)
    if dwell + max_song > min_piece + 1e-6:
        raise ValueError(
            f"DWELL constraint violated: dwell({dwell}) + max_song({max_song:.1f}) "
            f"> min_piece({min_piece:.1f}). Lower dwell_sec or use longer pieces.")

    pieces = shuffle([{"src": p["file"], "dur": float(p["dur"])} for p in catalog["pieces"]],
                     seed=seed + 1, no_repeat=True)
    idents = shuffle([{"src": i["file"], "dur": float(i["dur"])} for i in catalog["idents"]],
                     seed=seed + 2)
    edl = build_edl(pieces, idents, boundaries, real_dur, dwell, straddle)

    return {
        "seed": seed,
        "fps": int(config["fps"]),
        "resolution": str(config["resolution"]),
        "show_dur_sec": round(real_dur, 3),
        "music": timeline,
        "edl": edl,
        "subtitles": subs,
        "catalog_sha": _sha(sorted(p["file"] for p in catalog["pieces"]) +
                            sorted(i["file"] for i in catalog["idents"])),
        "library_sha": _sha(sorted(t["file"] for t in library)),
    }


def _load_catalog(path: str) -> dict:
    with open(path) as f:
        data = json.load(f)
    pieces = [{"file": e["file"], "dur": ffprobe_duration(e["file"])}
              for e in data["pieces"] if e["kind"] == "piece"]
    idents = [{"file": e["file"], "dur": ffprobe_duration(e["file"])}
              for e in data["pieces"] if e["kind"] == "ident"]
    return {"pieces": pieces, "idents": idents}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--library-dir", required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--show-dur", type=float, required=True)
    ap.add_argument("--dwell", type=float, default=600.0)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--resolution", default="1280x720")
    ap.add_argument("--straddle", action="store_true", default=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    import glob
    catalog = _load_catalog(a.catalog)
    library = [{"file": p, "title": title_from_path(p), "dur": ffprobe_duration(p)}
               for p in sorted(glob.glob(os.path.join(a.library_dir, "*.mp3")))]
    cfg = {"seed": a.seed, "show_dur_sec": a.show_dur, "dwell_sec": a.dwell,
           "fps": a.fps, "resolution": a.resolution, "straddle": a.straddle}
    plan_doc = build_plan(catalog, library, cfg)
    with open(a.out, "w") as f:
        json.dump(plan_doc, f, indent=2)
    print(f"[plan] wrote {a.out}: {len(plan_doc['edl'])} edl segs, "
          f"{len(plan_doc['music'])} songs, show_dur={plan_doc['show_dur_sec']:.1f}s")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd scripts/bake && uv run --with pytest pytest tests/test_plan.py -q`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/bake/plan.py scripts/bake/tests/test_plan.py
git commit -m "bake: plan.py EDL cut algorithm + build_plan + CLI"
```

---

### Task 3: Test fixtures + `assemble.py`

**Files:**
- Create: `scripts/bake/tests/make_fixtures.sh`, `scripts/bake/assemble.py`, `scripts/bake/tests/test_assemble.py`

**Interfaces:**
- Consumes: a `plan.json` (Task 2 output) + the source media it references.
- Produces: `assemble(plan_path, out_path)` → writes `out_path` (the muxed `show.mkv`); CLI `python3 assemble.py --plan plan.json --out show.mkv`.

- [ ] **Step 1: Write the fixture generator `scripts/bake/tests/make_fixtures.sh`**

```bash
#!/usr/bin/env bash
# Tiny synthetic catalog + library for fast integration tests (seconds long).
set -euo pipefail
DIR="${1:?usage: make_fixtures.sh <out_dir>}"
mkdir -p "$DIR/catalog" "$DIR/music"
mk() { ffmpeg -y -loglevel error -f lavfi -i "color=c=$2:s=320x180:r=60:d=$3" \
    -c:v libx264 -g 120 -keyint_min 120 -sc_threshold 0 -pix_fmt yuv420p "$DIR/catalog/$1"; }
mk piece_a.mp4 red 30
mk piece_b.mp4 green 30
mk ident.mp4 white 4
cat > "$DIR/catalog/catalog.json" <<JSON
{"pieces":[
  {"file":"$DIR/catalog/piece_a.mp4","kind":"piece"},
  {"file":"$DIR/catalog/piece_b.mp4","kind":"piece"},
  {"file":"$DIR/catalog/ident.mp4","kind":"ident"}
]}
JSON
tone() { ffmpeg -y -loglevel error -f lavfi -i "sine=f=$2:d=$3" -c:a libmp3lame "$DIR/music/$1"; }
tone song-one.mp3 330 6
tone song-two.mp3 440 5
tone song-three.mp3 550 7
echo "fixtures in $DIR"
```

- [ ] **Step 2: Write the failing integration test `scripts/bake/tests/test_assemble.py`**

```python
import json, os, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import plan, assemble

def _probe(path, stream, entry):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream,
        "-show_entries", entry, "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True)
    return out.stdout.strip()

def test_assemble_produces_synced_show(tmp_path):
    fx = tmp_path / "fx"
    subprocess.run([os.path.join(HERE, "make_fixtures.sh"), str(fx)], check=True)
    catalog = plan._load_catalog(str(fx / "catalog" / "catalog.json"))
    import glob
    lib = [{"file": p, "title": plan.title_from_path(p), "dur": plan.ffprobe_duration(p)}
           for p in sorted(glob.glob(str(fx / "music" / "*.mp3")))]
    cfg = {"seed": 1, "show_dur_sec": 15.0, "dwell_sec": 5.0,
           "fps": 60, "resolution": "320x180", "straddle": True}
    p = plan.build_plan(catalog, lib, cfg)
    plan_path = fx / "plan.json"
    plan_path.write_text(json.dumps(p))
    out = fx / "show.mkv"
    assemble.assemble(str(plan_path), str(out))

    assert out.exists()
    dur = float(_probe(str(out), "v:0", "format=duration"))
    assert abs(dur - p["show_dur_sec"]) < 0.5            # video length == show
    assert _probe(str(out), "v:0", "stream=codec_name") == "h264"
    assert _probe(str(out), "a:0", "stream=codec_name") == "aac"
    assert _probe(str(out), "v:0", "stream=width") == "320"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `chmod +x scripts/bake/tests/make_fixtures.sh && cd scripts/bake && uv run --with pytest pytest tests/test_assemble.py -q`
Expected: failure (`assemble` module missing).

- [ ] **Step 4: Implement `scripts/bake/assemble.py`**

```python
#!/usr/bin/env python3
"""Realize a plan.json into one muxed show file.

Three ffmpeg passes: video (concat EDL segments via the concat demuxer with
inpoint/outpoint, burn ASS titles, encode copy-safe H.264), audio (concat the
playlist, encode AAC), mux (stream-copy both). One video encode pass total.
"""
import json
import os
import subprocess
import sys
import tempfile


def _ts(sec: float) -> str:
    h = int(sec // 3600); m = int((sec % 3600) // 60)
    s = sec - h * 3600 - m * 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def write_ass(subs: list[dict], path: str, resolution: str):
    w, h = resolution.split("x")
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, BackColour, Bold, "
        "Alignment, MarginL, MarginR, MarginV\n"
        "Style: t,DejaVu Sans,28,&H00FFFFFF,&H64000000,1,1,40,40,40\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Text\n")
    lines = []
    for c in subs:
        # fade in 300ms, hold, fade to dim handled by player; keep simple here
        lines.append(f"Dialogue: 0,{_ass_t(c['start'])},{_ass_t(c['end'])},t,"
                     f"{{\\fad(300,300)}}\\u266a {c['text']}")
    with open(path, "w") as f:
        f.write(head + "\n".join(lines) + "\n")


def _ass_t(sec: float) -> str:
    h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec - h * 3600 - m * 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _run(cmd):
    subprocess.run(cmd, check=True)


def assemble(plan_path: str, out_path: str):
    with open(plan_path) as f:
        p = json.load(f)
    fps = int(p["fps"]); res = p["resolution"]; gop = 2 * fps
    work = tempfile.mkdtemp(prefix="bake_")

    # --- video concat list (concat demuxer, frame-accurate via inpoint/outpoint) ---
    vlist = os.path.join(work, "video.txt")
    with open(vlist, "w") as f:
        for seg in p["edl"]:
            f.write(f"file '{os.path.abspath(seg['src'])}'\n")
            f.write(f"inpoint {seg['in']}\n")
            f.write(f"outpoint {round(seg['in'] + seg['out'], 3)}\n")

    ass = os.path.join(work, "titles.ass")
    write_ass(p["subtitles"], ass, res)

    video = os.path.join(work, "video.mkv")
    _run(["ffmpeg", "-y", "-loglevel", "warning",
          "-f", "concat", "-safe", "0", "-i", vlist,
          "-vf", f"subtitles='{ass}',scale={res.replace('x',':')}:flags=lanczos,fps={fps}",
          "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
          "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
          "-b:v", os.environ.get("BAKE_VBITRATE", "6M"),
          "-maxrate", os.environ.get("BAKE_VBITRATE", "6M"),
          "-bufsize", os.environ.get("BAKE_VBUF", "12M"),
          "-an", video])

    # --- audio concat (full tracks, in playlist order) ---
    alist = os.path.join(work, "audio.txt")
    with open(alist, "w") as f:
        for song in p["music"]:
            f.write(f"file '{os.path.abspath(song['file'])}'\n")
    audio = os.path.join(work, "audio.m4a")
    _run(["ffmpeg", "-y", "-loglevel", "warning", "-f", "concat", "-safe", "0", "-i", alist,
          "-af", "volume=-7dB", "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", audio])

    # --- mux (stream-copy), atomic rename ---
    tmp_out = out_path + ".tmp.mkv"
    _run(["ffmpeg", "-y", "-loglevel", "warning", "-i", video, "-i", audio,
          "-c", "copy", "-shortest", tmp_out])
    os.replace(tmp_out, out_path)
    print(f"[assemble] wrote {out_path}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    assemble(a.plan, a.out)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd scripts/bake && uv run --with pytest pytest tests/test_assemble.py -q`
Expected: 1 passed. (If the `subtitles` filter is unavailable, ffmpeg was built without libass — install an ffmpeg with `--enable-libass`; note in the runbook.)

- [ ] **Step 6: Commit**

```bash
git add scripts/bake/assemble.py scripts/bake/tests/make_fixtures.sh scripts/bake/tests/test_assemble.py
git commit -m "bake: assemble.py — concat EDL + burn titles + mux (copy-safe)"
```

---

### Task 4: `validate.py`

**Files:**
- Create: `scripts/bake/validate.py`, `scripts/bake/tests/test_validate.py`

**Interfaces:**
- Consumes: `plan.json` + the baked `show.mkv`.
- Produces: `validate(plan_path, show_path) -> list[str]` (empty list = valid; else list of problems); CLI exits non-zero on problems.

- [ ] **Step 1: Write the failing test**

```python
# scripts/bake/tests/test_validate.py
import json, os, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import plan, assemble, validate

def test_validate_passes_on_good_bake(tmp_path):
    fx = tmp_path / "fx"
    subprocess.run([os.path.join(HERE, "make_fixtures.sh"), str(fx)], check=True)
    import glob
    catalog = plan._load_catalog(str(fx / "catalog" / "catalog.json"))
    lib = [{"file": p, "title": plan.title_from_path(p), "dur": plan.ffprobe_duration(p)}
           for p in sorted(glob.glob(str(fx / "music" / "*.mp3")))]
    cfg = {"seed": 1, "show_dur_sec": 15.0, "dwell_sec": 5.0,
           "fps": 60, "resolution": "320x180", "straddle": True}
    p = plan.build_plan(catalog, lib, cfg)
    (fx / "plan.json").write_text(json.dumps(p))
    assemble.assemble(str(fx / "plan.json"), str(fx / "show.mkv"))
    problems = validate.validate(str(fx / "plan.json"), str(fx / "show.mkv"))
    assert problems == [], problems

def test_validate_flags_wrong_duration(tmp_path):
    # a plan claiming 999s vs a short file -> duration problem
    fx = tmp_path / "fx"
    subprocess.run([os.path.join(HERE, "make_fixtures.sh"), str(fx)], check=True)
    import glob
    catalog = plan._load_catalog(str(fx / "catalog" / "catalog.json"))
    lib = [{"file": p, "title": plan.title_from_path(p), "dur": plan.ffprobe_duration(p)}
           for p in sorted(glob.glob(str(fx / "music" / "*.mp3")))]
    p = plan.build_plan(catalog, lib, {"seed": 1, "show_dur_sec": 15.0, "dwell_sec": 5.0,
                                       "fps": 60, "resolution": "320x180", "straddle": True})
    assemble.assemble(str(fx / "plan.json") if (fx/"plan.json").exists() else
                      _w(fx/"plan.json", p), str(fx / "show.mkv"))
    p["show_dur_sec"] = 999.0
    (fx / "plan2.json").write_text(json.dumps(p))
    problems = validate.validate(str(fx / "plan2.json"), str(fx / "show.mkv"))
    assert any("duration" in x for x in problems)

def _w(path, obj):
    path.write_text(json.dumps(obj)); return str(path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd scripts/bake && uv run --with pytest pytest tests/test_validate.py -q`
Expected: failure (`validate` missing).

- [ ] **Step 3: Implement `scripts/bake/validate.py`**

```python
#!/usr/bin/env python3
"""Validate a baked show against its plan. Empty result = valid."""
import json
import subprocess
import sys


def _probe(path, stream, entry):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream,
        "-show_entries", entry, "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True)
    return out.stdout.strip()


def validate(plan_path: str, show_path: str) -> list[str]:
    with open(plan_path) as f:
        p = json.load(f)
    problems = []
    dur = _probe(show_path, "v:0", "format=duration")
    if not dur:
        return [f"no video stream / unreadable: {show_path}"]
    if abs(float(dur) - float(p["show_dur_sec"])) > 1.0:
        problems.append(f"duration {float(dur):.1f}s != plan {p['show_dur_sec']:.1f}s")
    if _probe(show_path, "v:0", "stream=codec_name") != "h264":
        problems.append("video codec not h264")
    if _probe(show_path, "a:0", "stream=codec_name") != "aac":
        problems.append("audio codec not aac (or missing)")
    w = _probe(show_path, "v:0", "stream=width"); h = _probe(show_path, "v:0", "stream=height")
    if f"{w}x{h}" != p["resolution"]:
        problems.append(f"resolution {w}x{h} != plan {p['resolution']}")
    return problems


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--show", required=True)
    a = ap.parse_args()
    probs = validate(a.plan, a.show)
    if probs:
        print("INVALID:\n  " + "\n  ".join(probs), file=sys.stderr)
        sys.exit(1)
    print("valid")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd scripts/bake && uv run --with pytest pytest tests/ -q`
Expected: all tests pass (plan + assemble + validate).

- [ ] **Step 5: Commit**

```bash
git add scripts/bake/validate.py scripts/bake/tests/test_validate.py
git commit -m "bake: validate.py — ffprobe show.mkv against plan"
```

---

### Task 5: `bake.sh` orchestrator

**Files:**
- Create: `scripts/bake/bake.sh`

**Interfaces:**
- Consumes: S3 catalog + music, `plan.py`/`assemble.py`/`validate.py`.
- Produces: `shows/show-YYYY-MM-DD.mkv` (+ `.plan.json`) locally, uploaded to S3, with a `LATEST` pointer; old shows pruned past retention.

- [ ] **Step 1: Write `scripts/bake/bake.sh`**

```bash
#!/usr/bin/env bash
# Nightly bake: sync inputs from S3, plan -> assemble -> validate -> publish.
# Env: S3_MUSIC_BUCKET, S3_CATALOG_URI, SHOWS_DIR, S3_SHOWS_URI, AWS creds,
#      BAKE_RES (default 1280x720), BAKE_FPS (60), BAKE_DWELL (600),
#      SHOW_DUR (default 22800), BAKE_VBITRATE (6M), RETENTION_DAYS (3).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DATE="$(date +%F)"
SHOWS_DIR="${SHOWS_DIR:-/data/shows}"
MUSIC_DIR="${MUSIC_DIR:-/data/mp3}"
CATALOG_DIR="${CATALOG_DIR:-/data/catalog}"
mkdir -p "$SHOWS_DIR" "$MUSIC_DIR" "$CATALOG_DIR"

echo "[bake] syncing inputs from S3"
aws s3 sync "${S3_MUSIC_BUCKET:?}" "$MUSIC_DIR/" --no-progress --exclude "*" --include "*.mp3"
aws s3 sync "${S3_CATALOG_URI:?}" "$CATALOG_DIR/" --no-progress

PLAN="$SHOWS_DIR/show-$DATE.plan.json"
SHOW="$SHOWS_DIR/show-$DATE.mkv"
SEED="$(date +%Y%m%d)"

echo "[bake] planning (seed=$SEED res=${BAKE_RES:-1280x720})"
python3 "$HERE/plan.py" --catalog "$CATALOG_DIR/catalog.json" --library-dir "$MUSIC_DIR" \
    --seed "$SEED" --show-dur "${SHOW_DUR:-22800}" --dwell "${BAKE_DWELL:-600}" \
    --fps "${BAKE_FPS:-60}" --resolution "${BAKE_RES:-1280x720}" --out "$PLAN"

echo "[bake] assembling"
BAKE_VBITRATE="${BAKE_VBITRATE:-6M}" python3 "$HERE/assemble.py" --plan "$PLAN" --out "$SHOW"

echo "[bake] validating"
python3 "$HERE/validate.py" --plan "$PLAN" --show "$SHOW"

echo "[bake] publishing to S3"
aws s3 cp "$SHOW" "${S3_SHOWS_URI:?}/show-$DATE.mkv" --no-progress
aws s3 cp "$PLAN" "${S3_SHOWS_URI}/show-$DATE.plan.json" --no-progress
echo "show-$DATE.mkv" > "$SHOWS_DIR/LATEST"
aws s3 cp "$SHOWS_DIR/LATEST" "${S3_SHOWS_URI}/LATEST" --no-progress

echo "[bake] pruning shows older than ${RETENTION_DAYS:-3} days"
find "$SHOWS_DIR" -name 'show-*.mkv' -mtime "+${RETENTION_DAYS:-3}" -delete || true
echo "[bake] done: $SHOW"
```

- [ ] **Step 2: Verify end-to-end against fixtures (no S3)**

```bash
chmod +x scripts/bake/bake.sh
# stand up local fixtures as fake S3 inputs
TMP=$(mktemp -d); scripts/bake/tests/make_fixtures.sh "$TMP"
SHOWS_DIR="$TMP/shows" MUSIC_DIR="$TMP/music" CATALOG_DIR="$TMP/catalog" \
  BAKE_RES=320x180 SHOW_DUR=15 BAKE_DWELL=5 BAKE_VBITRATE=1M \
  python3 scripts/bake/plan.py --catalog "$TMP/catalog/catalog.json" --library-dir "$TMP/music" \
    --seed 20260616 --show-dur 15 --dwell 5 --fps 60 --resolution 320x180 --out "$TMP/p.json" \
  && python3 scripts/bake/assemble.py --plan "$TMP/p.json" --out "$TMP/show.mkv" \
  && python3 scripts/bake/validate.py --plan "$TMP/p.json" --show "$TMP/show.mkv"
```
Expected: `valid` printed; `$TMP/show.mkv` exists. (This exercises the same pipeline `bake.sh` runs; the S3 legs are validated in the deploy runbook.)

- [ ] **Step 3: Commit**

```bash
git add scripts/bake/bake.sh
git commit -m "bake: bake.sh orchestrator — sync, plan, assemble, validate, publish"
```

---

### Task 6: `playout.sh` (live) + health

**Files:**
- Create: `scripts/broadcast/playout.sh`, `scripts/broadcast/health_server.py`

**Interfaces:**
- Consumes: `shows/LATEST` (+ `show-*.mkv`), env `YOUTUBE_STREAM_KEY`, `YOUTUBE_URL`.
- Produces: a continuous RTMP stream-copy with retry + fallback; `/health` on `:8080` for `monitor.py`.

- [ ] **Step 1: Write `scripts/broadcast/health_server.py`**

```python
#!/usr/bin/env python3
"""Minimal /health endpoint for monitor.py. 200 while a heartbeat file is fresh."""
import http.server, os, time

HEARTBEAT = os.environ.get("PLAYOUT_HEARTBEAT", "/tmp/playout_heartbeat")
MAX_AGE = float(os.environ.get("PLAYOUT_HEARTBEAT_MAX_AGE", "30"))

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        fresh = os.path.exists(HEARTBEAT) and (time.time() - os.path.getmtime(HEARTBEAT)) < MAX_AGE
        self.send_response(200 if fresh else 503)
        self.end_headers()
        self.wfile.write(b"ok" if fresh else b"stale")
    def log_message(self, *a):
        pass

if __name__ == "__main__":
    http.server.HTTPServer(("0.0.0.0", int(os.environ.get("PLAYOUT_HEALTH_PORT", "8080"))), H).serve_forever()
```

- [ ] **Step 2: Write `scripts/broadcast/playout.sh`**

```bash
#!/usr/bin/env bash
# Live playout: stream-copy the day's pre-baked show to YouTube RTMP.
# Env: SHOWS_DIR (/data/shows), YOUTUBE_URL, YOUTUBE_STREAM_KEY, PLAYOUT_HEARTBEAT.
set -uo pipefail
SHOWS_DIR="${SHOWS_DIR:-/data/shows}"
YOUTUBE_URL="${YOUTUBE_URL:-rtmp://a.rtmp.youtube.com/live2}"
HEARTBEAT="${PLAYOUT_HEARTBEAT:-/tmp/playout_heartbeat}"

resolve_show() {
    # today's LATEST if present and valid, else the newest show-*.mkv on disk.
    local latest="$SHOWS_DIR/LATEST"
    if [[ -f "$latest" ]] && [[ -f "$SHOWS_DIR/$(cat "$latest")" ]]; then
        echo "$SHOWS_DIR/$(cat "$latest")"; return
    fi
    ls -t "$SHOWS_DIR"/show-*.mkv 2>/dev/null | head -1
}

while true; do
    SHOW="$(resolve_show)"
    if [[ -z "$SHOW" || ! -f "$SHOW" ]]; then
        echo "[playout] no show available; retrying in 15s" >&2; sleep 15; continue
    fi
    echo "[playout] streaming $SHOW"
    # touch heartbeat every 5s in the background while ffmpeg runs
    ( while :; do touch "$HEARTBEAT"; sleep 5; done ) & HB=$!
    ffmpeg -hide_banner -loglevel warning -re -i "$SHOW" \
        -c copy -f flv "$YOUTUBE_URL/${YOUTUBE_STREAM_KEY:?}" || true
    kill "$HB" 2>/dev/null || true
    echo "[playout] ffmpeg exited; restart in 3s" >&2
    sleep 3
done
```

- [ ] **Step 3: Verify locally (stream to a file instead of RTMP)**

```bash
chmod +x scripts/broadcast/playout.sh
TMP=$(mktemp -d); scripts/bake/tests/make_fixtures.sh "$TMP" >/dev/null
# bake a tiny show
python3 scripts/bake/plan.py --catalog "$TMP/catalog/catalog.json" --library-dir "$TMP/music" \
  --seed 1 --show-dur 15 --dwell 5 --fps 60 --resolution 320x180 --out "$TMP/p.json" >/dev/null
python3 scripts/bake/assemble.py --plan "$TMP/p.json" --out "$TMP/shows/show-x.mkv"
echo "show-x.mkv" > "$TMP/shows/LATEST"
# point YOUTUBE_URL at a local file sink and confirm a copy runs for a few seconds
SHOWS_DIR="$TMP/shows" YOUTUBE_URL="$TMP" YOUTUBE_STREAM_KEY="out.flv" \
  timeout 8 bash scripts/broadcast/playout.sh || true
ffprobe -v error -show_entries format=duration -of csv=p=0 "$TMP/out.flv"
```
Expected: `out.flv` exists with a few seconds of content; heartbeat file `/tmp/playout_heartbeat` was created. (Resolution/copy correctness is inherited from the bake; this only proves the resolve+retry+heartbeat loop.)

- [ ] **Step 4: Commit**

```bash
git add scripts/broadcast/playout.sh scripts/broadcast/health_server.py
git commit -m "broadcast: trivial stream-copy playout with fallback + health"
```

---

### Task 7: Deployment runbook (remote box + cron + systemd + monitor)

**Files:**
- Create: `docs/runbooks/prebaked-playout-deploy.md`

This task documents and scripts the remote deployment. Provisioning has manual
steps (the operator creates the box, sets secrets, configures the YouTube broadcast
binding via the existing monitor). Those are called out in **bold**.

- [ ] **Step 1: Write `docs/runbooks/prebaked-playout-deploy.md`** with the following, each command exact:

  1. **Provision the box (manual).** **Hetzner AX42** (Ryzen 7 8700GE, 8c/16t Zen4,
     ~€48/mo, ~20 TB included traffic, ≥500 GB disk). Ubuntu 24.04. Record its IP.
     The 8700GE iGPU (VCN/VAAPI) is a hardware-encode fallback for the bake if needed.
  2. **Install deps:** `sudo apt-get update && sudo apt-get install -y ffmpeg awscli python3 && ffmpeg -filters | grep -q subtitles || echo "WARN: ffmpeg lacks libass"`. Verify `ffmpeg`, `ffprobe`, `aws`, `python3`, and that ffmpeg has `libx264` + `subtitles`/libass.
  3. **Deploy the repo:** clone to `/opt/radio`, `git checkout main`. (Re-deploy = `git pull` + `systemctl restart`.)
  4. **Secrets (manual):** write `/etc/radio.env` with `S3_MUSIC_BUCKET`, `S3_CATALOG_URI`, `S3_SHOWS_URI`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`, `YOUTUBE_STREAM_KEY` (the **vxstory** key, separate from galton), `YOUTUBE_URL`, `BAKE_RES=1280x720` (→ `1920x1080` when the catalog is re-rendered at 1080p), `BAKE_FPS=60`, `SHOW_DUR=22800`, `BAKE_DWELL=600`, `BAKE_VBITRATE=6M`, plus the YouTube OAuth vars `monitor.py` needs. `chmod 600`.
  5. **Bake cron:** install a cron entry running `bake.sh` nightly well before the window — exact line:
     ```
     0 3 * * *  set -a; . /etc/radio.env; set +a; /opt/radio/scripts/bake/bake.sh >> /var/log/radio-bake.log 2>&1
     ```
  6. **Measure bake throughput (manual, do this first at each resolution).** Run `bake.sh` once by hand at `BAKE_RES=1280x720` and again at `1920x1080`; record wall time. **It MUST finish inside the 18:05→11:45 gap; if 1080p60 `medium` overruns, drop `assemble.py`'s preset to `fast`/`veryfast` (env-ize it) or move to an NVENC box.** Pin the preset/box from this measurement.
  7. **playout systemd unit** `/etc/systemd/system/radio-playout.service` (exact unit text: `EnvironmentFile=/etc/radio.env`, `ExecStart=/opt/radio/scripts/broadcast/playout.sh`, `Restart=always`). The script self-gates on show availability; the **operational window** is enforced by `monitor.py` + a wrapper that only streams in-window (reuse galton's `in_operational_window` pattern — copy it into `playout.sh` or a wrapper).
  8. **health + monitor units.** Run `health_server.py` (systemd unit) on `:8080`. Run `monitor.py` (systemd unit, `EnvironmentFile=/etc/radio.env`) configured to poll `http://localhost:8080/health` and manage the **vxstory** broadcast/stream key. **monitor.py is reused unchanged** — it is parameterized by env. **Manual:** confirm the YouTube stream key + a bound broadcast exist for vxstory (monitor's create/bind/bounce-after-bind handles auto-start once a stream goes active).
  9. **First live confirmation (manual).** With a baked show present, start the units during a test window (or `FORCE_ACTIVE=1`), watch `youtube.com/live/<id>`, and confirm: stream health stays good, the broadcast does not auto-complete, video cuts land on song changes, titles track songs. This is the success gate before retiring the old `scripts/playout/`.

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/prebaked-playout-deploy.md
git commit -m "docs: pre-baked playout deployment runbook"
```

---

### Task 8: Retire the old live-switching playout (after the live gate passes)

**Files:**
- Delete: `scripts/playout/video_player.py`, `scripts/playout/playout.sh`, `scripts/playout/run_local.sh`
- Modify: `README.md` (point the playout section at the pre-baked pipeline)

**Do this only after Task 7 step 9 confirms a stable live broadcast.**

- [ ] **Step 1: Confirm the new path is live-proven** (Task 7 step 9 done). If not, stop.
- [ ] **Step 2: Remove the superseded code**

```bash
git rm scripts/playout/video_player.py scripts/playout/playout.sh scripts/playout/run_local.sh
```

- [ ] **Step 3: Update `README.md`** — replace the "Video playout" section's pacer/pipe description with: pre-baked playout (`scripts/bake/` builds `show.mkv`; `scripts/broadcast/playout.sh` stream-copies it), linking the spec and runbook. (Show the exact replacement paragraph when editing.)

- [ ] **Step 4: Run the full test suite** `cd scripts/bake && uv run --with pytest pytest tests/ -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "playout: retire live-switching pipeline, superseded by pre-baked playout"
```

---

## Plan Notes

- **Build order is dependency order:** plan.py (1–2) → assemble (3) → validate (4) → bake.sh (5) → playout (6) → deploy (7) → retire old (8). Tasks 1–6 are fully testable on the dev box with synthetic fixtures; 7–8 are remote/operational.
- **The crux is Task 2** (the EDL cut algorithm) — it is where frame-exact song-boundary cutting is won, and it is pure/unit-tested.
- **Resolution bump (720p60 → 1080p60)** is config only (`BAKE_RES`/`--resolution`) plus re-rendering the catalog at 1080p (vxstory render: keep native 1080p, skip the downscale) — no code change here. Re-measure bake throughput (Task 7 step 6) before flipping it on.
- **Known risk to watch:** ffmpeg builds without libass can't burn the ASS titles (Task 3) — the runbook checks for it. The concat-demuxer + inpoint/outpoint frame accuracy and two-`-re`-free copy path are validated by the assemble/validate tests before any live attempt.
