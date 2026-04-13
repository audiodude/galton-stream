"""Shared IPC helpers for chat event services."""

import json
import os

EVENTS_FILE = os.environ.get("CHAT_EVENTS_FILE", "/tmp/chat_events.json")


def write_events(events):
    """Atomically merge events into the shared JSON file.

    Godot's reader deletes the file after reading, so any pre-existing
    entries here are events Godot hasn't consumed yet — merge, don't
    clobber, or we lose them when two writes land within one poll cycle.
    """
    existing = []
    try:
        with open(EVENTS_FILE) as f:
            parsed = json.load(f)
        if isinstance(parsed, list):
            existing = parsed
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    tmp = EVENTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(existing + list(events), f)
    os.replace(tmp, EVENTS_FILE)
