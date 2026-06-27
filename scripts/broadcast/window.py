#!/usr/bin/env python3
"""Operational-window gate for box playout. MIRRORS monitor/windows.py's
operational window (11:45-18:05 America/Los_Angeles, half-open) — KEEP IN SYNC.
CLI: exit 0 if in window, 1 if not (used by playout.sh: `python3 window.py && stream`)."""
import datetime
import sys
from zoneinfo import ZoneInfo

ACTIVE_TZ = ZoneInfo("America/Los_Angeles")
OPERATIONAL_START = (11, 45)
OPERATIONAL_END = (18, 5)


def in_operational_window(now=None):
    now = now or datetime.datetime.now(ACTIVE_TZ)
    return OPERATIONAL_START <= (now.hour, now.minute) < OPERATIONAL_END


if __name__ == "__main__":
    sys.exit(0 if in_operational_window() else 1)
