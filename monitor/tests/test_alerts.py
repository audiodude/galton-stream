import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import alerts

def make(monkeypatch):
    sent = []
    a = alerts.Alerter()
    monkeypatch.setattr(a, "send", lambda text: sent.append(text))
    return a, sent

def test_routine_transitions_do_not_page(monkeypatch):
    a, sent = make(monkeypatch)
    for st in ["OFF", "WAITING", "LIVE", "OFF"]:
        a.update(st, "routine")
    assert sent == []

def test_degraded_pages_once_and_recovery_pages(monkeypatch):
    a, sent = make(monkeypatch)
    a.update("LIVE", "ok")
    a.update("DEGRADED", "stream dropped")
    a.update("DEGRADED", "still down")   # no second page while still degraded
    a.update("LIVE", "recovered")
    assert len(sent) == 2
    assert "stream dropped" in sent[0] and "recovered" in sent[1]

def test_off_after_incident_counts_as_recovery(monkeypatch):
    a, sent = make(monkeypatch)
    a.update("LIVE", "ok"); a.update("DEGRADED", "drop"); a.update("OFF", "window close")
    assert len(sent) == 2
