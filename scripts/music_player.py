#!/usr/bin/env python3
"""Plays MP3s in shuffled order and pipes raw audio to a named pipe for FFmpeg.

Uses FFmpeg's concat demuxer to stitch the whole playlist into a single
persistent decoder process. Per-song subprocess startup was costing ~0.2s
of wall time at each boundary, which accumulated into multi-second audio/video
drift over a playlist cycle (see drift investigation notes).

Song transitions are detected by parsing the decoder's stderr for
"Opening 'X' for reading" messages, which fire exactly when libavformat
advances to the next input file. Byte-counting against ffprobe durations
was inaccurate enough (mp3 encoder delay/padding vs. predicted PCM length)
to accumulate ~11s of title lag across ~100 songs."""

import glob
import json
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time

MUSIC_DIR = os.environ.get("MUSIC_DIR", "/data/mp3")
AUDIO_PIPE = "/tmp/audio_pipe"
STATE_FILE = os.environ.get("STATE_FILE", "/data/playlist_state.json")
CONCAT_LIST = "/tmp/concat_list.txt"

BYTES_PER_SECOND = 44100 * 2 * 2  # 44.1kHz, stereo, s16le

OPENING_RE = re.compile(r"Opening '([^']+)' for reading")


def get_songs():
    songs = sorted(glob.glob(os.path.join(MUSIC_DIR, "*.mp3")))
    if not songs:
        print("ERROR: No MP3 files found in", MUSIC_DIR, file=sys.stderr)
        sys.exit(1)
    return songs


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
    # -loglevel verbose so libavformat emits "Opening 'X' for reading" on
    # each input switch; a background thread parses these to drive title
    # transitions.
    return subprocess.Popen([
        "ffmpeg", "-y",
        "-loglevel", "verbose",
        "-f", "concat", "-safe", "0",
        "-i", CONCAT_LIST,
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "-ar", "44100",
        "-ac", "2",
        "pipe:1",
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def stderr_reader(proc, playlist_set, transitions):
    """Parse decoder stderr, push matching file paths to the transition queue."""
    for raw in proc.stderr:
        try:
            line = raw.decode("utf-8", errors="replace")
        except Exception:
            continue
        m = OPENING_RE.search(line)
        if not m:
            continue
        path = m.group(1)
        if path in playlist_set:
            transitions.put(path)


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
        # Concat demuxer always reads from the top; there's no seek into the
        # middle of a concat stream. Rotate the playlist so start_index is 0.
        if start_index != 0:
            playlist = playlist[start_index:] + playlist[:start_index]
            start_index = 0

        write_concat_list(playlist)
        playlist_set = set(playlist)
        save_state(playlist, 0)
        current_idx = -1  # sentinel — first stderr transition fires DECODE_START

        proc = spawn_decoder()
        transitions: queue.Queue = queue.Queue()
        stderr_thread = threading.Thread(
            target=stderr_reader,
            args=(proc, playlist_set, transitions),
            daemon=True,
        )
        stderr_thread.start()

        cycle_start_wall = time.monotonic() - player_start
        total_bytes = 0

        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                os.write(pipe_fd, chunk)
                total_bytes += len(chunk)

                # Drain any pending transitions emitted by the stderr reader.
                while True:
                    try:
                        path = transitions.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        new_idx = playlist.index(path)
                    except ValueError:
                        continue
                    if new_idx != current_idx:
                        current_idx = new_idx
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
                    cycle_wall = now - cycle_start_wall
                    cycle_drift = audio_s - cycle_wall
                    print(f"[audio] CURSOR idx={current_idx} wall={now:.2f} "
                          f"audio_s={audio_s:.2f} "
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
              f"total_audio_s={total_bytes / BYTES_PER_SECOND:.2f}", flush=True)
        if proc.returncode != 0:
            print(f"Warning: decoder exited with {proc.returncode}",
                  file=sys.stderr, flush=True)

        random.shuffle(playlist)
        start_index = 0


if __name__ == "__main__":
    play_loop()
