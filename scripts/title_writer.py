#!/usr/bin/env python3
"""Writes current song title to a file on a wall-clock schedule.

Reads the playlist state file (shared with music_player.py) to know the
playlist order and current index. Uses ffprobe to get durations, then
sleeps through each song. Periodically checks the state file to re-sync
if the index has drifted from what we expect.
"""

import json
import os
import subprocess
import sys
import time

STATE_FILE = os.environ.get("STATE_FILE", "/data/playlist_state.json")
SONG_FILE = "/tmp/current_song.txt"
SYNC_INTERVAL = 30  # check state file for re-sync every N seconds


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
            capture_output=True, text=True, timeout=10)
        return float(result.stdout.strip())
    except (ValueError, AttributeError, subprocess.TimeoutExpired):
        return 0


def load_state():
    """Load playlist state. Returns (playlist, index) or (None, None)."""
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        return state["playlist"], state["index"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return None, None


def run():
    print("Title writer starting, waiting for playlist state...", flush=True)

    # Wait for music_player to create the state file
    while True:
        playlist, index = load_state()
        if playlist is not None:
            break
        time.sleep(2)

    print(f"Got playlist ({len(playlist)} tracks), starting at index {index}", flush=True)

    # Cache durations for the whole playlist
    durations = {}

    def get_cached_duration(path):
        if path not in durations:
            durations[path] = get_duration(path)
        return durations[path]

    current_index = index

    while True:
        # Write the current song title
        if current_index < len(playlist):
            song = playlist[current_index]
            title = song_title(song)
            write_song_name(title)
            duration = get_cached_duration(song)
            print(f"[title] WRITE '{title}' idx={current_index} duration={duration:.1f}s", flush=True)
        else:
            duration = 0
            print(f"[title] index {current_index} past end of playlist ({len(playlist)})", flush=True)

        # Sleep through the song, checking state file periodically for re-sync
        song_start = time.monotonic()
        elapsed = 0.0
        while elapsed < duration:
            sleep_time = min(SYNC_INTERVAL, duration - elapsed)
            time.sleep(sleep_time)
            elapsed += sleep_time

            # Check if music_player has moved to a different index
            playlist_now, index_now = load_state()
            if playlist_now is None:
                print(f"[title] sync check: state file missing, elapsed={elapsed:.1f}s", flush=True)
                continue

            if playlist_now != playlist:
                # Playlist was reshuffled
                print(f"[title] RESHUFFLE detected at elapsed={elapsed:.1f}s, state index={index_now}", flush=True)
                playlist = playlist_now
                durations.clear()
                current_index = index_now
                break

            if index_now != current_index:
                # Music player is on a different song than we expect
                wall = time.monotonic() - song_start
                print(f"[title] RESYNC: was idx={current_index}, state has idx={index_now}, "
                      f"wall={wall:.1f}s, expected={duration:.1f}s", flush=True)
                current_index = index_now
                break
            else:
                print(f"[title] sync ok: idx={current_index}, elapsed={elapsed:.1f}/{duration:.1f}s", flush=True)
        else:
            # Song duration elapsed normally, advance to next
            wall = time.monotonic() - song_start
            print(f"[title] ADVANCE: idx {current_index} -> {current_index + 1}, "
                  f"wall={wall:.1f}s, ffprobe={duration:.1f}s, drift={wall - duration:.2f}s", flush=True)
            current_index += 1
            if current_index >= len(playlist):
                # Wait for reshuffle
                print("End of playlist, waiting for reshuffle...", flush=True)
                while True:
                    time.sleep(2)
                    playlist_now, index_now = load_state()
                    if playlist_now is not None and (playlist_now != playlist or index_now == 0):
                        playlist = playlist_now
                        current_index = index_now
                        durations.clear()
                        print(f"New playlist ({len(playlist)} tracks)", flush=True)
                        break


if __name__ == "__main__":
    run()
