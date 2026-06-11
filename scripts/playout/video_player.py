#!/usr/bin/env python3
"""Decodes video catalog pieces to raw frames in a named pipe for FFmpeg.

Architecture mirrors music_player.py (audio pipe) but for video:
  - Reads catalog.json; splits entries into pieces[] and idents[].
  - Shuffled piece order; reshuffled when exhausted (avoid same piece twice
    in a row across reshuffles).
  - Watches SONG_FILE (default /tmp/current_song.txt) at 1 Hz for content
    changes. When the song changes AND elapsed >= DWELL_SEC, cuts to ident
    → next piece.
  - If a piece's decoder exits on its own before any boundary, chains
    directly to the next piece (no ident) — dead pipe is the only sin.
  - Uses -re so each piece is decoded at realtime, keeping song-boundary
    cuts possible (without -re the whole file dumps into the pipe instantly).
"""

import json
import os
import random
import signal
import subprocess
import sys
import threading
import time

CATALOG_DIR = os.environ.get("CATALOG_DIR", "./catalog")
VIDEO_PIPE   = os.environ.get("VIDEO_PIPE",  "/tmp/video_pipe")
SONG_FILE    = os.environ.get("SONG_FILE",   "/tmp/current_song.txt")
SONG_CLOCK   = os.environ.get("SONG_CLOCK",  "/tmp/song_clock.json")
AUDIO_PIPE_LEAD = 0.4  # seconds the audio pipe runs ahead of the mux
DWELL_SEC    = int(os.environ.get("DWELL_SEC", "900"))
FPS          = 60
SIZE         = "1280x720"

CHUNK = 65536
# One raw yuv420p frame. The pipe must ONLY ever contain whole frames: the
# master ffmpeg's rawvideo demuxer reads fixed-size frames, so a single
# partial frame (e.g. from killing a decoder mid-frame at a boundary cut)
# permanently shifts its read offset and scrambles everything after it.
_W, _H = (int(v) for v in SIZE.split("x"))
FRAME_BYTES = _W * _H * 3 // 2


def load_catalog():
    path = os.path.join(CATALOG_DIR, "catalog.json")
    with open(path) as f:
        data = json.load(f)
    pieces = [e for e in data["pieces"] if e["kind"] == "piece"]
    idents  = [e for e in data["pieces"] if e["kind"] == "ident"]
    if not pieces:
        print("ERROR: no pieces in catalog", file=sys.stderr, flush=True)
        sys.exit(1)
    if not idents:
        print("WARNING: no idents in catalog — transitions will skip ident step",
              file=sys.stderr, flush=True)
    return pieces, idents


def read_song_file():
    """Return content of SONG_FILE, or None if missing/unreadable."""
    try:
        with open(SONG_FILE) as f:
            return f.read().strip()
    except OSError:
        return None


def spawn_decoder(filepath):
    """Spawn ffmpeg decoding filepath → raw yuv420p frames on stdout at realtime (-re)."""
    return subprocess.Popen(
        [
            "ffmpeg", "-v", "error",
            "-re",
            "-i", filepath,
            "-f", "rawvideo",
            "-pix_fmt", "yuv420p",
            "-s", SIZE,
            "-r", str(FPS),
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
    )


def _write_all(pipe_fd, data):
    """os.write until all of data is written (large writes to pipes can be partial)."""
    view = memoryview(data)
    while view:
        n = os.write(pipe_fd, view)
        view = view[n:]


# Most recent full frame written to the pipe — replayed at 60 fps to bridge
# decoder-swap gaps. An empty pipe stalls the master ffmpeg, which underruns
# YouTube's player (visible as the stream "dying" at every transition).
_last_frame = [None]


def stream_to_pipe(proc, pipe_fd, stop_event, initial=b""):
    """Copy proc stdout → pipe_fd in WHOLE FRAMES until EOF, proc dies, or stop set.

    stop_event is honored only at frame boundaries, and a trailing partial
    frame at EOF/kill is dropped, so the pipe always stays frame-aligned.
    `initial` carries bytes already read during warmup.
    Returns 'eof' on natural end, 'interrupted' if stopped, 'pipe_lost' if a
    write fails (reader died).
    """
    buf = bytearray(initial)
    while not stop_event.is_set():
        try:
            while len(buf) >= FRAME_BYTES:
                frame = bytes(buf[:FRAME_BYTES])
                _write_all(pipe_fd, frame)
                _last_frame[0] = frame
                del buf[:FRAME_BYTES]
            chunk = proc.stdout.read(CHUNK)
            if not chunk:
                # EOF: drop any partial trailing frame rather than misalign the pipe.
                if buf:
                    print(f"[video] dropping {len(buf)} partial-frame bytes at EOF", flush=True)
                return "eof"
            buf.extend(chunk)
        except OSError as e:
            print(f"[video] pipe write error: {e}", flush=True)
            return "pipe_lost"
    return "interrupted"


def read_song_clock():
    """Read the music player's song clock: {file, started_at, duration, ends_at}.

    Returns None when missing, unparsable, or stale (ends_at well in the past
    — e.g. the music player died), so callers fall back to reactive cuts.
    """
    try:
        with open(SONG_CLOCK) as f:
            clock = json.load(f)
        if float(clock["ends_at"]) < time.time() - 5.0:
            return None
        return clock
    except (OSError, ValueError, KeyError):
        return None


def play_entry(entry, pipe_fd, watch_song=False, dwell_sec=DWELL_SEC, ident_dur=0.0):
    """Play one catalog entry into pipe_fd.

    If watch_song=True, cut at song boundaries (after dwell_sec of play):
    PREDICTIVELY when the song clock is available — the cut fires ident_dur
    seconds BEFORE the boundary so the ident straddles it and the next piece
    starts with the next song — or REACTIVELY (content change of SONG_FILE,
    ~1-6s late) when the clock is absent.
    Returns ('eof', elapsed) on natural file end, ('boundary', elapsed) on cut,
    or ('pipe_lost', elapsed) when the pipe write fails (reader died).
    """
    filepath = entry["file"]
    eid      = entry["id"]
    now_str  = time.strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[video] START id={eid} watch_song={watch_song} dwell_sec={dwell_sec} "
          f"ts={now_str}", flush=True)

    proc = spawn_decoder(filepath)
    _current_decoder_proc[0] = proc  # so the signal handler can kill it
    stop_event = threading.Event()
    start_mono = time.monotonic()
    start_wall = time.time()

    # Decoder warmup bridge: until this decoder yields its first full frame
    # (-re startup takes ~0.2-0.5s), keep the pipe fed by repeating the
    # previous piece's last frame at 60 fps. An empty pipe stalls the master
    # ffmpeg and underruns the live stream at every transition.
    warm = bytearray()
    out_fd = proc.stdout.fileno()
    os.set_blocking(out_fd, False)
    next_tick = time.monotonic()
    try:
        while len(warm) < FRAME_BYTES:
            chunk = proc.stdout.read(CHUNK)
            if chunk:
                warm.extend(chunk)
                continue
            if chunk == b"":
                break  # decoder exited before producing a frame
            if _last_frame[0] is not None:
                now = time.monotonic()
                if now >= next_tick:
                    _write_all(pipe_fd, _last_frame[0])
                    next_tick = max(next_tick + 1.0 / FPS, now - 0.1)
                else:
                    time.sleep(min(next_tick - now, 0.005))
            else:
                time.sleep(0.005)
    except OSError as e:
        print(f"[video] pipe write error during warmup: {e}", flush=True)
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        print(f"[video] END id={eid} reason=pipe_lost elapsed=0.0s", flush=True)
        return "pipe_lost", 0.0
    os.set_blocking(out_fd, True)

    # Stream in a background thread so the main thread can poll SONG_FILE.
    stream_result = [None]  # mutable container for thread return value
    def _stream():
        stream_result[0] = stream_to_pipe(proc, pipe_fd, stop_event, initial=bytes(warm))
    t = threading.Thread(target=_stream, daemon=True)
    t.start()

    last_song = read_song_file() if watch_song else None
    reason = "eof"

    try:
        while t.is_alive():
            # 100ms poll: EOF detection latency is dead air in the video pipe
            # (the bridge only runs during the NEXT entry's warmup), and at
            # 1s it was draining YouTube's buffer at every ident transition.
            time.sleep(0.1)
            elapsed = time.monotonic() - start_mono

            # Check if the streaming thread detected a pipe failure.
            if stream_result[0] == "pipe_lost":
                reason = "pipe_lost"
                stop_event.set()
                proc.terminate()
                t.join(timeout=5)
                break

            if watch_song:
                clock = read_song_clock()
                if clock is not None:
                    # Predictive: cut ident_dur early so the ident straddles the
                    # boundary. AUDIO_PIPE_LEAD compensates for audio sitting in
                    # its pipe ~0.4s ahead of the mux.
                    boundary = float(clock["ends_at"]) + AUDIO_PIPE_LEAD
                    now_wall = time.time()
                    if (boundary - start_wall) >= dwell_sec and now_wall >= boundary - ident_dur:
                        reason = "boundary"
                        print(f"[video] SCHED_CUT boundary_in={boundary - now_wall:.1f}s "
                              f"ident_dur={ident_dur:.1f}s song={clock.get('file', '?')}",
                              flush=True)
                        stop_event.set()
                        proc.terminate()
                        t.join(timeout=5)
                        break
                else:
                    # Reactive fallback: notice the song already changed.
                    current_song = read_song_file()
                    if current_song != last_song and elapsed >= dwell_sec:
                        reason = "boundary"
                        stop_event.set()
                        proc.terminate()
                        t.join(timeout=5)
                        break
                    # Update last_song even if dwell not reached, so the *next*
                    # eligible check catches the boundary that's already been crossed.
                    if current_song != last_song:
                        last_song = current_song

        # If the thread finished naturally, check for pipe_lost set at EOF path.
        if reason == "eof" and stream_result[0] == "pipe_lost":
            reason = "pipe_lost"
    finally:
        stop_event.set()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    elapsed = time.monotonic() - start_mono
    print(f"[video] END id={eid} reason={reason} elapsed={elapsed:.1f}s", flush=True)
    return reason, elapsed


def make_shuffled_order(pieces, last_id=None):
    """Return a shuffled list of piece indices. Avoid starting with last_id."""
    order = list(range(len(pieces)))
    random.shuffle(order)
    if last_id is not None and order and pieces[order[0]]["id"] == last_id:
        # Swap first with a random other position to avoid back-to-back.
        if len(order) > 1:
            swap = random.randint(1, len(order) - 1)
            order[0], order[swap] = order[swap], order[0]
    return order


def pick_ident(idents):
    """Pick a random ident entry, or None if no idents."""
    if not idents:
        return None
    return random.choice(idents)


_shutdown = threading.Event()
_current_decoder_proc = [None]  # for SIGTERM handler


def _handle_signal(signum, frame):
    print(f"[video] received signal {signum}, shutting down", flush=True)
    _shutdown.set()
    p = _current_decoder_proc[0]
    if p is not None:
        try:
            p.terminate()
        except OSError:
            pass


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    pieces, idents = load_catalog()
    print(f"[video] catalog loaded: {len(pieces)} pieces, {len(idents)} idents",
          flush=True)

    # Create VIDEO_PIPE if needed.
    if not os.path.exists(VIDEO_PIPE):
        os.mkfifo(VIDEO_PIPE)
        print(f"[video] created pipe {VIDEO_PIPE}", flush=True)

    print(f"[video] waiting for reader to open {VIDEO_PIPE} ...", flush=True)
    # os.open with O_WRONLY blocks until the read end is opened — that's correct.
    pipe_fd = os.open(VIDEO_PIPE, os.O_WRONLY)
    print(f"[video] pipe open, starting playback", flush=True)

    order    = make_shuffled_order(pieces)
    idx      = 0
    last_id  = None

    while not _shutdown.is_set():
        if idx >= len(order):
            order = make_shuffled_order(pieces, last_id=last_id)
            idx   = 0

        entry  = pieces[order[idx]]
        # Pre-pick the transition ident: the predictive scheduler needs its
        # duration to start the cut early enough for the ident to straddle
        # the song boundary.
        next_ident = pick_ident(idents)
        next_ident_dur = float(next_ident["duration_sec"]) if next_ident else 0.0
        reason, _ = play_entry(entry, pipe_fd, watch_song=True, dwell_sec=DWELL_SEC,
                               ident_dur=next_ident_dur)

        if _shutdown.is_set():
            break

        if reason == "pipe_lost":
            # Reader (master ffmpeg) died. close the broken fd, wait for a new reader.
            print(f"[video] pipe_lost — closing fd and waiting for new reader on "
                  f"{VIDEO_PIPE} ...", flush=True)
            try:
                os.close(pipe_fd)
            except OSError:
                pass
            # Block until the new reader opens the pipe (start.sh restarts ffmpeg).
            pipe_fd = os.open(VIDEO_PIPE, os.O_WRONLY)
            print("[video] pipe reopened, resuming playback (replaying current piece)",
                  flush=True)
            # Do NOT advance idx — replay the same piece from the beginning.
            continue

        last_id = entry["id"]
        idx    += 1

        if reason in ("boundary", "eof"):
            # Every piece transition gets an ident — it's what makes a cut
            # read as intentional. Play the PRE-PICKED ident: the scheduler
            # timed the cut so this exact ident's duration lands the next
            # piece on the song boundary.
            if next_ident:
                play_entry(next_ident, pipe_fd, watch_song=False)

    os.close(pipe_fd)
    print("[video] shutdown complete", flush=True)


if __name__ == "__main__":
    main()
