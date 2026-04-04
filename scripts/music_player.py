#!/usr/bin/env python3
"""Plays MP3s in shuffled order, writes current song to a file,
and pipes raw audio to a named pipe for FFmpeg."""

import glob
import json
import os
import random
import subprocess
import sys
import time

MUSIC_DIR = os.environ.get("MUSIC_DIR", "/data/mp3")
AUDIO_PIPE = "/tmp/audio_pipe"
SONG_FILE = "/tmp/current_song.txt"
STATE_FILE = "/data/playlist_state.json"


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

    while True:
        for i in range(start_index, len(playlist)):
            song = playlist[i]
            save_state(playlist, i)
            title = song_title(song)
            write_song_name(title)
            print(f"Now playing: {title} ({i + 1}/{len(playlist)})", flush=True)

            # Decode MP3 to raw PCM and write to the named pipe
            # FFmpeg reads from the pipe
            proc = subprocess.run([
                "ffmpeg", "-y",
                "-i", song,
                "-f", "s16le",
                "-acodec", "pcm_s16le",
                "-ar", "44100",
                "-ac", "2",
                AUDIO_PIPE
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            if proc.returncode != 0:
                print(f"Warning: failed to play {title}", file=sys.stderr, flush=True)

        # Reshuffle for next loop
        random.shuffle(playlist)
        start_index = 0


if __name__ == "__main__":
    play_loop()
