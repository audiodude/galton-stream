# monitor/tests/test_executor.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor

def test_execute_maps_actions(monkeypatch):
    calls = []
    monkeypatch.setattr(monitor.youtube, "ensure_broadcast", lambda sid: calls.append(("create", sid)))
    monkeypatch.setattr(monitor.youtube, "go_live", lambda bid: calls.append(("go_live", bid)))
    monkeypatch.setattr(monitor.youtube, "end_broadcast", lambda bid: calls.append(("end", bid)))
    monkeypatch.setattr(monitor.youtube, "delete_broadcast", lambda bid: calls.append(("del", bid)))
    monkeypatch.setattr(monitor.redirect, "set_radio_online", lambda vid: calls.append(("on", vid)))
    monkeypatch.setattr(monitor.redirect, "set_radio_offline", lambda: calls.append(("off",)))
    monitor.STREAM_ID = "S1"
    monitor.execute(("create_broadcast",))
    monitor.execute(("go_live", "B1"))
    monitor.execute(("end_broadcast", "B1"))
    monitor.execute(("delete_broadcast", "B2"))
    monitor.execute(("redirect_online", "VID"))
    monitor.execute(("redirect_offline",))
    assert calls == [("create", "S1"), ("go_live", "B1"), ("end", "B1"),
                     ("del", "B2"), ("on", "VID"), ("off",)]

def test_classify_states():
    assert monitor.classify(False, False, None, False, 0) == "OFF"
    assert monitor.classify(True, True, {"id": "B"}, True, 0) == "LIVE"
    assert monitor.classify(True, True, None, False, 0) == "WAITING"
    assert monitor.classify(True, True, None, False, 5) == "DEGRADED"   # past grace
