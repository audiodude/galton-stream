#!/usr/bin/env python3
"""Writes current song title to a file, driven by music_player's state.

FFmpeg muxes audio and video by PTS, so writing the title at the same
wall-clock moment we start decoding the song keeps the title and audio
in sync automatically. No buffering math, no schedule — just react to
music_player's state file.
"""

import json
import os
import time

STATE_FILE = os.environ.get("STATE_FILE", "/data/playlist_state.json")
SONG_FILE = "/tmp/current_song.txt"
POLL_INTERVAL = 0.2


def song_title(path):
    name = os.path.splitext(os.path.basename(path))[0]
    return name.replace("-", " ").replace("_", " ").title()


def write_song_name(title):
    tmp = SONG_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(title)
    os.replace(tmp, SONG_FILE)


def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        return state["playlist"], state["index"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return None, None


def run():
    print("Title writer starting, waiting for playlist state...", flush=True)

    start_mono = time.monotonic()
    last_index = None
    last_playlist = None

    while True:
        playlist, index = load_state()
        if playlist is not None and 0 <= index < len(playlist):
            if index != last_index or playlist != last_playlist:
                title = song_title(playlist[index])
                write_song_name(title)
                wall = time.monotonic() - start_mono
                print(f"[title] WRITE idx={index} wall={wall:.2f} '{title}'",
                      flush=True)
                last_index = index
                last_playlist = playlist
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
