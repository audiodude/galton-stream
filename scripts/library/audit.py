#!/usr/bin/env python3
# audit.py <workdir>
# Scans every mp3 in <workdir>/original/ for mastered-in leading/trailing silence.
# Uses ffmpeg silencedetect + ffprobe (no extra dependencies).
#
# Outputs:
#   <workdir>/audit.json   — per-track record (file, duration, leading_s, trailing_s)
#   stdout                 — human-readable table sorted by trailing_s desc, offenders flagged
#
# Offender thresholds (edit here):
TRAILING_THRESHOLD_S = 4.0   # trailing silence > this flags the track
LEADING_THRESHOLD_S  = 3.0   # leading silence  > this flags the track

# Silence detection parameters (matches ffmpeg silencedetect defaults used in production):
SILENCE_NOISE  = "-45dB"
SILENCE_DUR    = "2"         # minimum silence duration in seconds

import json
import os
import re
import subprocess
import sys
from pathlib import Path

def get_duration(path: str) -> float:
    """Return total duration in seconds via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def detect_silence(path: str) -> list[dict]:
    """
    Run ffmpeg silencedetect and return list of silence regions:
      [{"start": float, "end": float}, ...]
    """
    result = subprocess.run(
        [
            "ffmpeg", "-i", path,
            "-af", f"silencedetect=n={SILENCE_NOISE}:d={SILENCE_DUR}",
            "-f", "null", "-",
        ],
        capture_output=True, text=True,
    )
    # silencedetect writes to stderr
    output = result.stderr

    regions = []
    current_start = None
    for line in output.splitlines():
        m_start = re.search(r"silence_start:\s*([\d.]+)", line)
        m_end   = re.search(r"silence_end:\s*([\d.]+)", line)
        if m_start:
            current_start = float(m_start.group(1))
        if m_end and current_start is not None:
            regions.append({"start": current_start, "end": float(m_end.group(1))})
            current_start = None
    # If a silence_start was seen but no matching silence_end, it runs to EOF.
    # We'll handle that case using duration in the caller.
    if current_start is not None:
        regions.append({"start": current_start, "end": None})

    return regions


def audit_file(path: str) -> dict:
    """Return audit record for a single mp3."""
    duration = get_duration(path)
    regions   = detect_silence(path)

    leading_s  = 0.0
    trailing_s = 0.0

    for r in regions:
        start = r["start"]
        end   = r["end"] if r["end"] is not None else duration

        # Leading silence: a region that starts at (or very near) 0
        if start < 0.05:
            leading_s = max(leading_s, end)

        # Trailing silence: a region whose end coincides with the file end within 0.5s
        if abs(end - duration) <= 0.5:
            trailing_s = max(trailing_s, end - start)

    return {
        "file":       os.path.basename(path),
        "duration":   round(duration, 3),
        "leading_s":  round(leading_s, 3),
        "trailing_s": round(trailing_s, 3),
    }


def main():
    if len(sys.argv) != 2:
        print("usage: audit.py <workdir>", file=sys.stderr)
        sys.exit(1)

    workdir   = Path(sys.argv[1])
    orig_dir  = workdir / "original"
    audit_out = workdir / "audit.json"

    if not orig_dir.is_dir():
        print(f"error: {orig_dir} does not exist — run sync_down.sh first", file=sys.stderr)
        sys.exit(1)

    mp3s = sorted(orig_dir.glob("*.mp3"))
    if not mp3s:
        print(f"error: no mp3 files found in {orig_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"[audit] scanning {len(mp3s)} mp3s in {orig_dir} ...")

    records = []
    errors  = []
    for i, mp3 in enumerate(mp3s, 1):
        print(f"  [{i}/{len(mp3s)}] {mp3.name}", end=" ", flush=True)
        try:
            rec = audit_file(str(mp3))
            records.append(rec)
            flag = ""
            if rec["trailing_s"] > TRAILING_THRESHOLD_S:
                flag += " TRAILING"
            if rec["leading_s"] > LEADING_THRESHOLD_S:
                flag += " LEADING"
            print(f"dur={rec['duration']:.1f}s  trail={rec['trailing_s']:.1f}s  lead={rec['leading_s']:.1f}s{flag}")
        except Exception as e:
            print(f"ERROR: {e}")
            errors.append({"file": mp3.name, "error": str(e)})

    # Write audit.json
    audit_data = {"records": records, "errors": errors}
    with open(audit_out, "w") as f:
        json.dump(audit_data, f, indent=2)
    print(f"\n[audit] wrote {audit_out}")

    # Summary table
    offenders = [r for r in records if r["trailing_s"] > TRAILING_THRESHOLD_S or r["leading_s"] > LEADING_THRESHOLD_S]
    offenders.sort(key=lambda r: r["trailing_s"], reverse=True)

    print(f"\n{'='*72}")
    print(f"AUDIT SUMMARY: {len(records)} tracks scanned, {len(offenders)} offenders")
    print(f"  Trailing silence threshold: > {TRAILING_THRESHOLD_S}s")
    print(f"  Leading silence threshold:  > {LEADING_THRESHOLD_S}s")
    if errors:
        print(f"  Errors: {len(errors)}")
    print(f"{'='*72}")

    if offenders:
        hdr = f"  {'FILE':<40} {'DUR':>7} {'TRAIL':>7} {'LEAD':>6}  FLAGS"
        print(hdr)
        print(f"  {'-'*67}")
        for r in offenders:
            flags = []
            if r["trailing_s"] > TRAILING_THRESHOLD_S:
                flags.append("TRAILING")
            if r["leading_s"] > LEADING_THRESHOLD_S:
                flags.append("LEADING")
            print(f"  {r['file']:<40} {r['duration']:>7.1f} {r['trailing_s']:>7.1f} {r['leading_s']:>6.1f}  {', '.join(flags)}")
    else:
        print("  No offenders found.")
    print()

    if errors:
        print(f"ERRORS ({len(errors)}):")
        for e in errors:
            print(f"  {e['file']}: {e['error']}")
        print()

    return len(offenders)


if __name__ == "__main__":
    sys.exit(0 if main() == 0 else 0)  # always exit 0; caller inspects audit.json
