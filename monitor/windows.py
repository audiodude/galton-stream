"""Operational/consumer window math (America/Los_Angeles). Pure: pass `now`."""
import datetime
import os
from zoneinfo import ZoneInfo

ACTIVE_TZ = ZoneInfo("America/Los_Angeles")
OPERATIONAL_START = (11, 45)
CONSUMER_START = (12, 0)
CONSUMER_END = (18, 0)
OPERATIONAL_END = (18, 5)

FORCE_ACTIVE = os.environ.get("FORCE_ACTIVE", "") == "1"


def _in_window(start_hm, end_hm, now=None):
    now = now or datetime.datetime.now(ACTIVE_TZ)
    hm = (now.hour, now.minute)
    return start_hm <= hm < end_hm


def in_operational_window(now=None):
    return FORCE_ACTIVE or _in_window(OPERATIONAL_START, OPERATIONAL_END, now)


def in_consumer_window(now=None):
    return FORCE_ACTIVE or _in_window(CONSUMER_START, CONSUMER_END, now)


def seconds_until_next_boundary(now=None):
    """Seconds to the next of the four window edges, so the loop wakes within a
    second of 12:00 / 18:00 / etc."""
    now = now or datetime.datetime.now(ACTIVE_TZ)
    today = now.date()
    tomorrow = today + datetime.timedelta(days=1)
    candidates = []
    for h, m in (OPERATIONAL_START, CONSUMER_START, CONSUMER_END, OPERATIONAL_END):
        for day in (today, tomorrow):
            t = datetime.datetime.combine(day, datetime.time(h, m), tzinfo=ACTIVE_TZ)
            if t > now:
                candidates.append((t - now).total_seconds())
    return min(candidates) if candidates else float("inf")
