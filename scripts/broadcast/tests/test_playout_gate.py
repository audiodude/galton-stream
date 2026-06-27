# scripts/broadcast/tests/test_playout_gate.py
import os, sys, subprocess, tempfile, textwrap, time, signal, pathlib

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYOUT = os.path.join(os.path.dirname(HERE), "playout.sh")

def _run_playout(env, secs=4):
    p = subprocess.Popen(["bash", PLAYOUT], env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
    time.sleep(secs)
    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
    try: p.wait(timeout=10)
    except subprocess.TimeoutExpired: os.killpg(os.getpgid(p.pid), signal.SIGKILL)

def _base_env(tmp, in_window: bool):
    shows = pathlib.Path(tmp) / "shows"; shows.mkdir()
    (shows / "show-x.mkv").write_text("fake")
    (shows / "LATEST").write_text("show-x.mkv")
    marker = pathlib.Path(tmp) / "ff_ran"
    fake_ff = pathlib.Path(tmp) / "ffmpeg"
    fake_ff.write_text("#!/usr/bin/env bash\ntouch '%s'\nsleep 30\n" % marker)
    fake_ff.chmod(0o755)
    env = dict(os.environ)
    env.update({
        "SHOWS_DIR": str(shows), "PLAYOUT_HEARTBEAT": str(pathlib.Path(tmp) / "hb"),
        "YOUTUBE_STREAM_KEY": "k", "YOUTUBE_URL": "rtmp://example/live",
        "FFMPEG_BIN": str(fake_ff), "SUPERVISE_INTERVAL": "1",
        "WINDOW_CMD": "true" if in_window else "false",
    })
    return env, marker

def test_out_of_window_does_not_start_ffmpeg():
    with tempfile.TemporaryDirectory() as tmp:
        env, marker = _base_env(tmp, in_window=False)
        _run_playout(env, secs=3)
        assert not marker.exists()   # ffmpeg never invoked outside the window

def test_in_window_starts_ffmpeg():
    with tempfile.TemporaryDirectory() as tmp:
        env, marker = _base_env(tmp, in_window=True)
        _run_playout(env, secs=3)
        assert marker.exists()       # ffmpeg invoked inside the window
