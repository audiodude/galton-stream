# scripts/broadcast/tests/test_window.py
import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import window
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
def at(mon, day, h, m): return datetime.datetime(2026, mon, day, h, m, tzinfo=PT)

def test_boundaries_dst_summer():           # June = PDT
    assert not window.in_operational_window(at(6, 24, 11, 44))
    assert window.in_operational_window(at(6, 24, 11, 45))   # inclusive start
    assert window.in_operational_window(at(6, 24, 18, 4))
    assert not window.in_operational_window(at(6, 24, 18, 5)) # exclusive end

def test_boundaries_standard_winter():      # January = PST
    assert not window.in_operational_window(at(1, 15, 11, 44))
    assert window.in_operational_window(at(1, 15, 12, 0))
    assert not window.in_operational_window(at(1, 15, 18, 5))
