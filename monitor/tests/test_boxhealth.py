import os, sys, io, urllib.error
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import boxhealth

class FakeResp:
    def __init__(self, payload): self._p = payload
    def read(self): return self._p
    def __enter__(self): return self
    def __exit__(self, *a): return False

def test_probe_returns_json(monkeypatch):
    monkeypatch.setattr(boxhealth, "BOX_HEALTH_URL", "https://box/health")
    monkeypatch.setattr(boxhealth, "BOX_HEALTH_TOKEN", "tok")
    captured = {}
    def fake_urlopen(req, timeout=0):
        captured["auth"] = req.headers.get("Authorization")
        return FakeResp(b'{"playout_alive": true, "ffmpeg_alive": true, "heartbeat_age_s": 2}')
    monkeypatch.setattr(boxhealth.urllib.request, "urlopen", fake_urlopen)
    out = boxhealth.probe()
    assert out["ffmpeg_alive"] is True and out["heartbeat_age_s"] == 2
    assert captured["auth"] == "Bearer tok"

def test_probe_unreachable_returns_none(monkeypatch):
    monkeypatch.setattr(boxhealth, "BOX_HEALTH_URL", "https://box/health")
    def boom(req, timeout=0): raise urllib.error.URLError("nope")
    monkeypatch.setattr(boxhealth.urllib.request, "urlopen", boom)
    assert boxhealth.probe() is None

def test_probe_no_url_returns_none(monkeypatch):
    monkeypatch.setattr(boxhealth, "BOX_HEALTH_URL", "")
    assert boxhealth.probe() is None
