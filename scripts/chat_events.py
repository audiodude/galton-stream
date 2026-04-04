"""Shared IPC helpers for chat event services."""

import json
import os

EVENTS_FILE = os.environ.get("CHAT_EVENTS_FILE", "/tmp/chat_events.json")


def write_events(events):
    """Atomically write events to the shared JSON file."""
    tmp = EVENTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(events, f)
    os.replace(tmp, EVENTS_FILE)
