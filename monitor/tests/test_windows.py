import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import windows
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
def at(h, m): return datetime.datetime(2026, 6, 19, h, m, tzinfo=PT)

def test_operational_window_bounds():
    assert not windows.in_operational_window(at(11, 44))
    assert windows.in_operational_window(at(11, 45))      # inclusive start
    assert windows.in_operational_window(at(18, 4))
    assert not windows.in_operational_window(at(18, 5))   # exclusive end

def test_consumer_window_bounds():
    assert not windows.in_consumer_window(at(11, 59))
    assert windows.in_consumer_window(at(12, 0))
    assert not windows.in_consumer_window(at(18, 0))

def test_operational_brackets_consumer():
    # 11:45-12:00 warmup and 18:00-18:05 cooldown are operational but not consumer
    assert windows.in_operational_window(at(11, 50)) and not windows.in_consumer_window(at(11, 50))
    assert windows.in_operational_window(at(18, 2)) and not windows.in_consumer_window(at(18, 2))

def test_seconds_until_next_boundary_positive_and_bounded():
    s = windows.seconds_until_next_boundary(at(13, 0))
    assert 0 < s <= 24 * 3600
