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
