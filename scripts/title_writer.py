#!/usr/bin/env python3
"""Writes current song title to a file, synced to what the viewer hears.

Reads the playlist state file written by music_player.py. The state file
includes audio_buffer_seconds: how far ahead the decoder is of wall-clock
playback. When we see a new index, we schedule the title write for
now + audio_buffer_seconds so it lands when the viewer actually hears the
song start.

Purely reactive — no prediction, no accumulator.
"""

import json
import os
import time
from collections import deque

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
        return (
            state["playlist"],
            state["index"],
            state.get("audio_buffer_seconds", 0.0),
        )
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return None, None, 0.0


def run():
    print("Title writer starting, waiting for playlist state...", flush=True)

    last_seen_index = None
    last_seen_playlist = None
    pending = deque()  # (target_monotonic, title, index)

    while True:
        playlist, index, buffer_seconds = load_state()
        now = time.monotonic()

        if playlist is not None and 0 <= index < len(playlist):
            changed = (
                last_seen_index is None
                or index != last_seen_index
                or playlist != last_seen_playlist
            )
            if changed:
                title = song_title(playlist[index])
                target = now + max(0.0, buffer_seconds)
                pending.append((target, title, index))
                print(f"[title] QUEUE idx={index} buffer={buffer_seconds:.2f}s "
                      f"-> '{title}' in {target - now:.2f}s", flush=True)
                last_seen_index = index
                last_seen_playlist = playlist

        while pending and pending[0][0] <= now:
            target, title, idx = pending.popleft()
            write_song_name(title)
            late = now - target
            print(f"[title] WRITE idx={idx} '{title}' late={late:.2f}s", flush=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
