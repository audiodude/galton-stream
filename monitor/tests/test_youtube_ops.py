import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import youtube

class Recorder:
    def __init__(self, responses): self.responses, self.calls = responses, []
    def __call__(self, url, method="GET", body=None):
        self.calls.append((method, url, body))
        for frag, resp in self.responses.items():
            if frag in url: return resp
        return {}

def test_get_owned_stream_id_matches_key(monkeypatch):
    monkeypatch.setattr(youtube, "YOUTUBE_STREAM_KEY", "live2-abc")
    youtube._owned_stream_id = None
    rec = Recorder({"liveStreams": {"items": [
        {"id": "S_other", "cdn": {"ingestionInfo": {"streamName": "nope"}}},
        {"id": "S_mine", "cdn": {"ingestionInfo": {"streamName": "live2-abc"}}},
    ]}})
    monkeypatch.setattr(youtube, "api", rec)
    assert youtube.get_owned_stream_id() == "S_mine"

def test_stream_status(monkeypatch):
    rec = Recorder({"liveStreams": {"items": [
        {"id": "S1", "status": {"streamStatus": "active", "healthStatus": {"status": "good"}}}]}})
    monkeypatch.setattr(youtube, "api", rec)
    assert youtube.stream_status("S1") == ("active", "good")

def test_ensure_broadcast_sets_autostop_false_autostart_false(monkeypatch):
    rec = Recorder({"liveBroadcasts?part=snippet,status,contentDetails": {"id": "B1"},
                    "/bind": {"id": "B1"}})
    monkeypatch.setattr(youtube, "api", rec)
    bid = youtube.ensure_broadcast("S1")
    assert bid == "B1"
    create = next(c for c in rec.calls if c[0] == "POST" and "part=snippet,status,contentDetails" in c[1])
    cd = create[2]["contentDetails"]
    assert cd["enableAutoStart"] is False and cd["enableAutoStop"] is False
    assert cd["enableDvr"] is False and cd["latencyPreference"] == "ultraLow"
    # with a monitor stream, ready -> live is rejected 403 invalidTransition
    assert cd["monitorStream"]["enableMonitorStream"] is False
    assert any("/bind" in c[1] and "streamId=S1" in c[1] for c in rec.calls)

def test_go_live_transitions_to_live(monkeypatch):
    rec = Recorder({"/transition": {"id": "B1"}})
    monkeypatch.setattr(youtube, "api", rec)
    assert youtube.go_live("B1") is True
    assert any("broadcastStatus=live" in c[1] and "id=B1" in c[1] for c in rec.calls)

def test_go_live_falls_back_to_testing_when_live_is_rejected(monkeypatch):
    """Broadcasts carrying a monitor stream must pass through `testing`."""
    calls = []
    def api(url, method="GET", body=None):
        calls.append(url)
        return None if "broadcastStatus=live" in url else {"id": "B1"}
    monkeypatch.setattr(youtube, "api", api)
    assert youtube.go_live("B1") is True
    assert any("broadcastStatus=live" in u for u in calls)
    assert any("broadcastStatus=testing" in u for u in calls)

def test_end_broadcast_completes_then_private(monkeypatch):
    rec = Recorder({"/transition": {"id": "B1"},
                    "liveBroadcasts?part=snippet,status&id=B1": {"items": [
                        {"id": "B1", "snippet": {"title": "t", "scheduledStartTime": "1970-01-01T00:00:00Z"}}]},
                    "liveBroadcasts?part=snippet,status": {"id": "B1"}})
    monkeypatch.setattr(youtube, "api", rec)
    assert youtube.end_broadcast("B1") is True
    assert any("broadcastStatus=complete" in c[1] for c in rec.calls)
    assert any(c[0] == "PUT" and c[2] and c[2].get("status", {}).get("privacyStatus") == "private" for c in rec.calls)

def test_ensure_broadcast_deletes_on_bind_failure(monkeypatch):
    rec = Recorder({"liveBroadcasts?part=snippet,status,contentDetails": {"id": "B1"},
                    "/bind": None})  # bind returns None (failure)
    monkeypatch.setattr(youtube, "api", rec)
    monkeypatch.setattr(youtube, "delete_broadcast", lambda bid: rec.calls.append(("DELETE", bid, None)))
    bid = youtube.ensure_broadcast("S1")
    assert bid is None
    assert any(c == ("DELETE", "B1", None) for c in rec.calls)
