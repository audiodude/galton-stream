#!/usr/bin/env python3
"""Plays MP3s in shuffled order and pipes raw audio to a named pipe for FFmpeg.

Uses FFmpeg's concat demuxer to stitch the whole playlist into a single
persistent decoder process. Per-song subprocess startup was costing ~0.2s
of wall time at each boundary, which accumulated into multi-second audio/video
drift over a playlist cycle (see drift investigation notes).

Song boundaries are detected from the cumulative PCM byte count using
precomputed song durations from ffprobe. State is still written to the
playlist state file so title_writer.py can react to track changes."""

import bisect
import glob
import json
import os
import random
import subprocess
import sys
import time

MUSIC_DIR = os.environ.get("MUSIC_DIR", "/data/mp3")
AUDIO_PIPE = "/tmp/audio_pipe"
STATE_FILE = os.environ.get("STATE_FILE", "/data/playlist_state.json")
CONCAT_LIST = "/tmp/concat_list.txt"

BYTES_PER_SECOND = 44100 * 2 * 2  # 44.1kHz, stereo, s16le


def get_songs():
    songs = sorted(glob.glob(os.path.join(MUSIC_DIR, "*.mp3")))
    if not songs:
        print("ERROR: No MP3 files found in", MUSIC_DIR, file=sys.stderr)
        sys.exit(1)
    return songs


def probe_duration(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ], text=True).strip()
    return float(out)


def probe_playlist(playlist):
    """Return list of per-song byte lengths aligned to playlist order."""
    print(f"Probing {len(playlist)} tracks for duration...", flush=True)
    t0 = time.monotonic()
    bytelens = []
    for p in playlist:
        try:
            dur = probe_duration(p)
        except Exception as e:
            print(f"  probe failed for {os.path.basename(p)}: {e}",
                  file=sys.stderr, flush=True)
            dur = 0.0
        bytelens.append(int(round(dur * BYTES_PER_SECOND)))
    print(f"Probe complete in {time.monotonic() - t0:.1f}s", flush=True)
    return bytelens


def write_concat_list(playlist):
    with open(CONCAT_LIST, "w") as f:
        for p in playlist:
            escaped = p.replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")


def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        playlist = state["playlist"]
        index = state["index"]
        if all(os.path.exists(s) for s in playlist):
            print(f"Resuming playlist at track {index + 1}/{len(playlist)}", flush=True)
            return playlist, index
        print("Playlist files changed, reshuffling", flush=True)
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        pass
    return None, 0


def save_state(playlist, index):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"playlist": playlist, "index": index}, f)
    os.replace(tmp, STATE_FILE)


def spawn_decoder():
    # No -re: decoder runs at disk speed, pipe backpressure paces output.
    # Downstream ffmpeg's audio encoder is muxed with real-time x11grab video,
    # so it drains the pipe at exactly 44.1kHz stereo = 176400 B/s. Upstream
    # blocks on a full 64KB pipe, yielding tight real-time pacing with no
    # per-file timing artifacts.
    return subprocess.Popen([
        "ffmpeg", "-y",
        "-loglevel", "warning",
        "-f", "concat", "-safe", "0",
        "-i", CONCAT_LIST,
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "-ar", "44100",
        "-ac", "2",
        "pipe:1",
    ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def play_loop():
    if os.path.exists(AUDIO_PIPE):
        os.remove(AUDIO_PIPE)
    os.mkfifo(AUDIO_PIPE)

    all_songs = get_songs()
    print(f"Found {len(all_songs)} tracks", flush=True)

    playlist, start_index = load_state()
    if playlist is None:
        playlist = all_songs[:]
        random.shuffle(playlist)
        start_index = 0

    # Open pipe once and keep it open across decoder restarts. If we close
    # and reopen per-cycle, the streaming ffmpeg sees EOF and stalls.
    pipe_fd = os.open(AUDIO_PIPE, os.O_WRONLY)
    print("Audio pipe opened for writing", flush=True)

    player_start = time.monotonic()
    last_cursor_log = -10.0

    while True:
        bytelens = probe_playlist(playlist)
        # Cumulative byte offsets: boundaries[i] = first byte of song i.
        # boundaries[len] = total playlist size.
        boundaries = [0]
        for b in bytelens:
            boundaries.append(boundaries[-1] + b)
        total_playlist_bytes = boundaries[-1]

        write_concat_list(playlist)

        # Concat demuxer always reads from the top; there's no seek into the
        # middle of a concat stream without probing offsets into individual
        # files. Simpler to just re-start the cycle from start_index by
        # building a rotated playlist.
        if start_index != 0:
            playlist = playlist[start_index:] + playlist[:start_index]
            bytelens = bytelens[start_index:] + bytelens[:start_index]
            boundaries = [0]
            for b in bytelens:
                boundaries.append(boundaries[-1] + b)
            write_concat_list(playlist)
            start_index = 0

        save_state(playlist, 0)
        current_idx = 0
        print(f"[audio] DECODE_START idx=0 wall={time.monotonic() - player_start:.2f} "
              f"audio_s=0.00 {os.path.basename(playlist[0])} "
              f"(1/{len(playlist)})", flush=True)

        proc = spawn_decoder()
        cycle_start_bytes_wall = time.monotonic() - player_start
        cycle_start_total_written = 0
        total_bytes = 0  # bytes written in current cycle
        song_start_bytes = 0

        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                os.write(pipe_fd, chunk)
                total_bytes += len(chunk)

                # Detect song boundary transitions.
                new_idx = bisect.bisect_right(boundaries, total_bytes) - 1
                if new_idx >= len(playlist):
                    new_idx = len(playlist) - 1
                if new_idx != current_idx:
                    current_idx = new_idx
                    song_start_bytes = boundaries[current_idx]
                    save_state(playlist, current_idx)
                    wall = time.monotonic() - player_start
                    print(f"[audio] DECODE_START idx={current_idx} wall={wall:.2f} "
                          f"audio_s={total_bytes / BYTES_PER_SECOND:.2f} "
                          f"{os.path.basename(playlist[current_idx])} "
                          f"({current_idx + 1}/{len(playlist)})", flush=True)

                now = time.monotonic() - player_start
                if now - last_cursor_log >= 10.0:
                    last_cursor_log = now
                    audio_s = total_bytes / BYTES_PER_SECOND
                    song_pos = (total_bytes - song_start_bytes) / BYTES_PER_SECOND
                    # Drift relative to cycle start
                    cycle_wall = now - cycle_start_bytes_wall
                    cycle_drift = audio_s - cycle_wall
                    print(f"[audio] CURSOR idx={current_idx} wall={now:.2f} "
                          f"audio_s={audio_s:.2f} song_pos={song_pos:.2f} "
                          f"cycle_drift={cycle_drift:+.3f}", flush=True)
        except BrokenPipeError:
            print("Broken pipe writing audio — reader likely died, waiting to retry...",
                  file=sys.stderr, flush=True)
            proc.kill()
            proc.wait()
            os.close(pipe_fd)
            time.sleep(10)
            pipe_fd = os.open(AUDIO_PIPE, os.O_WRONLY)
            print("Audio pipe reopened", flush=True)
            continue

        proc.wait()
        end_wall = time.monotonic() - player_start
        print(f"[audio] CYCLE_END wall={end_wall:.2f} "
              f"total_audio_s={total_bytes / BYTES_PER_SECOND:.2f} "
              f"expected={total_playlist_bytes / BYTES_PER_SECOND:.2f}", flush=True)
        if proc.returncode != 0:
            print(f"Warning: decoder exited with {proc.returncode}",
                  file=sys.stderr, flush=True)

        random.shuffle(playlist)
        start_index = 0


if __name__ == "__main__":
    play_loop()
