#!/usr/bin/env python3
"""Plays MP3s in shuffled order, writes current song to a file,
and pipes raw audio to a named pipe for FFmpeg."""

import glob
import json
import os
import queue
import random
import subprocess
import sys
import threading
import time

MUSIC_DIR = os.environ.get("MUSIC_DIR", "/data/mp3")
AUDIO_PIPE = "/tmp/audio_pipe"
SONG_FILE = "/tmp/current_song.txt"
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


def song_title(path):
    """Convert filename to display title."""
    name = os.path.splitext(os.path.basename(path))[0]
    return name.replace("-", " ").replace("_", " ").title()


def write_song_name(title):
    tmp = SONG_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(title)
    os.replace(tmp, SONG_FILE)


def get_duration(path):
    """Get track duration in seconds via ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True)
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        return 0


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

    # Queue of (absolute_time, title) — a background thread processes
    # them in order, sleeping until each one is due.
    title_queue = queue.Queue()

    def title_worker():
        while True:
            show_at, title = title_queue.get()
            wait = show_at - time.time()
            if wait > 0:
                time.sleep(wait)
            write_song_name(title)
            print(f"Now playing: {title}", flush=True)

    t = threading.Thread(target=title_worker, daemon=True)
    t.start()

    title_available_at = 0.0

    # Open the pipe for writing and keep it open across songs.
    # If each decoder opens/closes the pipe directly, the streaming
    # FFmpeg sees EOF at every song boundary and stalls.
    pipe_fd = os.open(AUDIO_PIPE, os.O_WRONLY)
    print("Audio pipe opened for writing", flush=True)

    while True:
        for i in range(start_index, len(playlist)):
            song = playlist[i]
            save_state(playlist, i)
            title = song_title(song)
            duration = get_duration(song)

            # Queue the title to display when this song actually starts
            # playing (after the previous song's real-time duration).
            show_at = max(title_available_at, time.time())
            title_queue.put((show_at, title))
            print(f"Decoding: {title} ({i + 1}/{len(playlist)}, {duration:.0f}s)", flush=True)

            title_available_at = show_at + duration

            # Decode MP3 to raw PCM, piping stdout through our
            # persistent pipe fd so it never sees EOF between songs.
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
                title_available_at = 0.0
                continue

            proc.wait()
            if proc.returncode != 0:
                print(f"Warning: failed to play {title}", file=sys.stderr, flush=True)

        # Reshuffle for next loop
        random.shuffle(playlist)
        start_index = 0


if __name__ == "__main__":
    play_loop()
