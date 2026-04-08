#!/usr/bin/env python3
"""Plays MP3s in shuffled order and pipes raw audio to a named pipe for FFmpeg.

Title display is handled by title_writer.py, which reads the playlist state
file and schedules titles independently using ffprobe durations."""

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


def get_songs():
    songs = sorted(glob.glob(os.path.join(MUSIC_DIR, "*.mp3")))
    if not songs:
        print("ERROR: No MP3 files found in", MUSIC_DIR, file=sys.stderr)
        sys.exit(1)
    return songs


def load_state():
    """Load saved playlist order and current index."""
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        playlist = state["playlist"]
        index = state["index"]
        # Verify all files still exist
        if all(os.path.exists(s) for s in playlist):
            print(f"Resuming playlist at track {index + 1}/{len(playlist)}", flush=True)
            return playlist, index
        print("Playlist files changed, reshuffling", flush=True)
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        pass
    return None, 0


def save_state(playlist, index):
    """Save current playlist order and index."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"playlist": playlist, "index": index}, f)
    os.replace(tmp, STATE_FILE)


def play_loop():
    # Create named pipe
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

    # Open the pipe for writing and keep it open across songs.
    # If each decoder opens/closes the pipe directly, the streaming
    # FFmpeg sees EOF at every song boundary and stalls.
    pipe_fd = os.open(AUDIO_PIPE, os.O_WRONLY)
    print("Audio pipe opened for writing", flush=True)

    while True:
        for i in range(start_index, len(playlist)):
            song = playlist[i]
            save_state(playlist, i)
            print(f"[audio] DECODE idx={i} {os.path.basename(song)} ({i + 1}/{len(playlist)})", flush=True)

            proc = subprocess.Popen([
                "ffmpeg", "-y",
                "-re",
                "-i", song,
                "-f", "s16le",
                "-acodec", "pcm_s16le",
                "-ar", "44100",
                "-ac", "2",
                "pipe:1"
            ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

            try:
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    os.write(pipe_fd, chunk)
            except BrokenPipeError:
                print(f"Broken pipe writing audio — reader likely died, waiting to retry...",
                      file=sys.stderr, flush=True)
                proc.kill()
                proc.wait()
                os.close(pipe_fd)
                # Wait for streaming FFmpeg to restart, then reopen pipe
                time.sleep(10)
                pipe_fd = os.open(AUDIO_PIPE, os.O_WRONLY)
                print("Audio pipe reopened", flush=True)
                continue

            proc.wait()
            if proc.returncode != 0:
                print(f"Warning: failed to play {os.path.basename(song)}", file=sys.stderr, flush=True)

        # Reshuffle for next loop
        random.shuffle(playlist)
        start_index = 0


if __name__ == "__main__":
    play_loop()
