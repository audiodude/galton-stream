# scripts/broadcast/tests/test_health_server.py
import os, sys, json, threading, urllib.request, urllib.error, http.server
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import health_server as hs

def test_payload_in_window_streaming():
    p = hs.health_payload(lambda: True, lambda: 2, lambda: True)
    assert p == {"playout_alive": True, "ffmpeg_alive": True, "heartbeat_age_s": 2}

def test_payload_in_window_ffmpeg_down():
    p = hs.health_payload(lambda: False, lambda: 99, lambda: True)
    assert p["playout_alive"] is False and p["ffmpeg_alive"] is False

def test_payload_out_of_window_is_idle_ok():
    p = hs.health_payload(lambda: False, lambda: -1, lambda: False)
    assert p["playout_alive"] is True and p["heartbeat_age_s"] == -1

def _serve(monkeypatch_token):
    hs.TOKEN = monkeypatch_token
    srv = http.server.HTTPServer(("127.0.0.1", 0), hs.H)
    threading.Thread(target=srv.handle_request, daemon=True)  # one per call below
    return srv

def _get(srv, auth=None):
    port = srv.server_address[1]
    req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
    if auth: req.add_header("Authorization", auth)
    t = threading.Thread(target=srv.handle_request); t.start()
    try:
        r = urllib.request.urlopen(req, timeout=5); t.join(); return r.status, r.read()
    except urllib.error.HTTPError as e:
        t.join(); return e.code, e.read()

def test_http_401_without_token():
    srv = _serve("secret"); code, _ = _get(srv); assert code == 401

def test_http_401_wrong_token():
    srv = _serve("secret"); code, _ = _get(srv, "Bearer nope"); assert code == 401

def test_http_200_with_token():
    srv = _serve("secret")
    code, body = _get(srv, "Bearer secret")
    assert code == 200
    d = json.loads(body); assert set(d) == {"playout_alive", "ffmpeg_alive", "heartbeat_age_s"}
