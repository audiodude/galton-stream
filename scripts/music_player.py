#!/usr/bin/env python3
"""Plays MP3s in shuffled order, writes current song to a file,
and pipes raw audio to a named pipe for FFmpeg."""

import glob
import os
import random
import subprocess
import sys
import time

MUSIC_DIR = os.environ.get("MUSIC_DIR", "/data/mp3")
AUDIO_PIPE = "/tmp/audio_pipe"
SONG_FILE = "/tmp/current_song.txt"


def get_songs():
    songs = sorted(glob.glob(os.path.join(MUSIC_DIR, "*.mp3")))
    if not songs:
        print("ERROR: No MP3 files found in", MUSIC_DIR, file=sys.stderr)
        sys.exit(1)
    return songs


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

    songs = get_songs()
    print(f"Found {len(songs)} tracks", flush=True)

    while True:
        random.shuffle(songs)
        for song in songs:
            title = song_title(song)
            write_song_name(title)
            print(f"Now playing: {title}", flush=True)

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


if __name__ == "__main__":
    play_loop()
