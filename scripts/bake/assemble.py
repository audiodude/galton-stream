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
        # fade in 300ms; the note must be the literal ♪ glyph — ASS has no
        # \u escape, so "♪" would render verbatim (the original bug).
        lines.append(f"Dialogue: 0,{_ass_t(c['start'])},{_ass_t(c['end'])},t,"
                     f"{{\\fad(300,300)}}♪ {c['text']}")
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
