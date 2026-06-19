import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import youtube
from zoneinfo import ZoneInfo

UTC = datetime.timezone.utc

def _b(bid, life, bound, sst=None):
    return {"id": bid,
            "status": {"lifeCycleStatus": life},
            "contentDetails": {"boundStreamId": bound},
            "snippet": {"scheduledStartTime": sst} if sst else {}}

def test_owned_broadcasts_filters_by_bound_stream():
    bs = [_b("mine", "live", "S1"), _b("theirs", "live", "S2"), _b("mine2", "ready", "S1")]
    owned = youtube.owned_broadcasts(bs, "S1")
    assert [b["id"] for b in owned] == ["mine", "mine2"]

def test_owned_broadcasts_excludes_unbound():
    bs = [{"id": "x", "status": {"lifeCycleStatus": "live"}, "contentDetails": {}}]
    assert youtube.owned_broadcasts(bs, "S1") == []

def test_life_default_empty():
    assert youtube.life({"status": {}}) == ""
    assert youtube.life(_b("a", "testing", "S1")) == "testing"

def test_is_recent():
    now = datetime.datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    fresh = _b("a", "ready", "S1", sst="2026-06-19T11:55:00Z")   # 5 min ago
    stale = _b("b", "ready", "S1", sst="2026-06-19T11:30:00Z")   # 30 min ago
    assert youtube.is_recent(fresh, now)
    assert not youtube.is_recent(stale, now)
    assert not youtube.is_recent(_b("c", "ready", "S1"), now)    # no sst

def test_get_access_token_caches(monkeypatch):
    calls = {"n": 0}
    class FakeResp:
        def read(self): return b'{"access_token": "tok", "expires_in": 3600}'
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=0):
        calls["n"] += 1
        return FakeResp()
    monkeypatch.setattr(youtube.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(youtube, "YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setattr(youtube, "YOUTUBE_REFRESH_TOKEN", "rt")
    youtube._access_token = None
    youtube._token_expires = 0
    assert youtube.get_access_token() == "tok"
    assert youtube.get_access_token() == "tok"   # cached
    assert calls["n"] == 1
