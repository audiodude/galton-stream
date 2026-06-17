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
