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
DWELL_SEC    = int(os.environ.get("DWELL_SEC", "900"))
FPS          = 60
SIZE         = "1280x720"

CHUNK = 65536


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


def stream_to_pipe(proc, pipe_fd, stop_event):
    """Copy proc stdout → pipe_fd in chunks until EOF, proc dies, or stop_event set.

    Returns 'eof' if the decoder ran to natural EOF, 'interrupted' if stopped,
    or 'pipe_lost' if a write to the pipe fails (reader died).
    """
    while not stop_event.is_set():
        try:
            chunk = proc.stdout.read(CHUNK)
            if not chunk:
                return "eof"   # natural EOF
            os.write(pipe_fd, chunk)
        except OSError as e:
            print(f"[video] pipe write error: {e}", flush=True)
            return "pipe_lost"
    return "interrupted"


def play_entry(entry, pipe_fd, watch_song=False, dwell_sec=DWELL_SEC):
    """Play one catalog entry into pipe_fd.

    If watch_song=True, monitor SONG_FILE; return ('boundary', elapsed) when
    content changes AND elapsed >= dwell_sec, interrupting playback.
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

    # Stream in a background thread so the main thread can poll SONG_FILE.
    stream_result = [None]  # mutable container for thread return value
    def _stream():
        stream_result[0] = stream_to_pipe(proc, pipe_fd, stop_event)
    t = threading.Thread(target=_stream, daemon=True)
    t.start()

    last_song = read_song_file() if watch_song else None
    reason = "eof"

    try:
        while t.is_alive():
            time.sleep(1.0)
            elapsed = time.monotonic() - start_mono

            # Check if the streaming thread detected a pipe failure.
            if stream_result[0] == "pipe_lost":
                reason = "pipe_lost"
                stop_event.set()
                proc.terminate()
                t.join(timeout=5)
                break

            if watch_song:
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
        reason, _ = play_entry(entry, pipe_fd, watch_song=True, dwell_sec=DWELL_SEC)

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

        if reason == "boundary":
            # Song boundary: play one ident fully, then continue piece loop.
            ident = pick_ident(idents)
            if ident:
                play_entry(ident, pipe_fd, watch_song=False)

        # reason == "eof": chain directly into next piece (no ident).

    os.close(pipe_fd)
    print("[video] shutdown complete", flush=True)


if __name__ == "__main__":
    main()
