#!/usr/bin/env python3
"""Decodes video catalog pieces to raw frames in a named pipe for FFmpeg.

Architecture mirrors music_player.py (audio pipe) but for video:
  - Reads catalog.json; splits entries into pieces[] and idents[].
  - Shuffled piece order; reshuffled when exhausted (avoid same piece twice
    in a row across reshuffles).
  - Cuts at song boundaries (after DWELL_SEC of play), predictively when the
    music player's song clock is available, reactively otherwise.

Pacer model (the supply invariant)
----------------------------------
A single wall-clock PACER thread owns every write to the video pipe, emitting
the most recent decoded frame at 60fps from a latest-frame slot. Decoders run
under -re and feed the slot; they never touch the pipe.

Why: the master ffmpeg's video input must never run dry. Previously each piece
wrote the pipe directly, so a decoder swap (transition) or a decoder pacing
slightly under realtime left the pipe momentarily empty — ffmpeg then blocked
on the read, dropping output below realtime and slowly draining YouTube's
player buffer (the "stream dies after ~30s" / videoIngestionStarved symptom).

The pacer fixes this structurally:
  - Floor: the slot is ALWAYS full (black frame, then last decoded frame), so
    ffmpeg never blocks on an empty video pipe. Backpressure from ffmpeg
    (paced realtime by the audio -re input) sets the write rate.
  - Ceiling: a wall-clock sleep keeps us from exceeding 60fps if the pipe
    drains in a burst.
  - Continuity: across decoder swaps/warmups/EOF the slot keeps its last
    frame, so supply never dips.
This is the same invariant galton gets for free from x11grab, which samples
the display at a fixed wall-clock rate regardless of render state.
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
# One raw yuv420p frame. The slot must ONLY ever hold whole frames: the master
# ffmpeg's rawvideo demuxer reads fixed-size frames, so a single partial frame
# permanently shifts its read offset and scrambles everything after it.
_W, _H = (int(v) for v in SIZE.split("x"))
FRAME_BYTES = _W * _H * 3 // 2
# yuv420p black: Y=0 plane, U=V=128 planes.
_BLACK_FRAME = bytes(_W * _H) + bytes(b"\x80") * (_W * _H // 2)


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


# Latest-frame slot: the decoder feed publishes whole frames here; the pacer
# samples it at 60fps. Reference assignment is atomic under the GIL, so no lock
# is needed for the single-producer/single-consumer handoff.
_latest_frame = [_BLACK_FRAME]
_pipe_lost = threading.Event()
_shutdown = threading.Event()
_current_decoder_proc = [None]  # for the signal handler


def pacer_loop(pipe_fd):
    """Own all pipe writes: emit the latest frame at a wall-clock-capped 60fps.

    Runs until shutdown or until a write fails (reader died), which sets
    _pipe_lost so the main loop can reopen the pipe and restart a fresh pacer.
    """
    period = 1.0 / FPS
    next_t = time.monotonic()
    while not _shutdown.is_set():
        try:
            _write_all(pipe_fd, _latest_frame[0])
        except OSError as e:
            print(f"[video] pacer pipe write error: {e}", flush=True)
            _pipe_lost.set()
            return
        next_t += period
        ahead = next_t - time.monotonic()
        if ahead > 0:
            time.sleep(ahead)
        else:
            next_t = time.monotonic()  # fell behind; resync, don't burst to catch up


def feed_slot(proc, stop_event):
    """Read whole frames from the decoder and publish each into the latest slot.

    Only complete frames reach the slot (partial trailing bytes are held until
    they fill). Returns 'eof' on natural decoder end, 'interrupted' if stopped.
    """
    buf = bytearray()
    while not stop_event.is_set():
        chunk = proc.stdout.read(CHUNK)
        if not chunk:
            return "eof"
        buf.extend(chunk)
        while len(buf) >= FRAME_BYTES:
            _latest_frame[0] = bytes(buf[:FRAME_BYTES])
            del buf[:FRAME_BYTES]
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


def play_entry(entry, watch_song=False, dwell_sec=DWELL_SEC, ident_dur=0.0):
    """Run one catalog entry's decoder, feeding the latest-frame slot.

    The pacer (not this function) writes the pipe, so a decoder swap here never
    interrupts supply. If watch_song=True, cut at song boundaries (after
    dwell_sec of play): PREDICTIVELY when the song clock is available — the cut
    fires ident_dur seconds BEFORE the boundary so the ident straddles it and
    the next piece starts with the next song — or REACTIVELY (content change of
    SONG_FILE) when the clock is absent.
    Returns ('eof', elapsed) on natural file end, ('boundary', elapsed) on cut,
    ('pipe_lost', elapsed) if the pacer reported a dead reader, or
    ('interrupted', elapsed) on shutdown.
    """
    filepath = entry["file"]
    eid      = entry["id"]
    now_str  = time.strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[video] START id={eid} watch_song={watch_song} dwell_sec={dwell_sec} "
          f"ts={now_str}", flush=True)

    proc = spawn_decoder(filepath)
    _current_decoder_proc[0] = proc
    stop_event = threading.Event()
    start_mono = time.monotonic()
    start_wall = time.time()

    feed_result = [None]
    def _run_feed():
        feed_result[0] = feed_slot(proc, stop_event)
    t = threading.Thread(target=_run_feed, daemon=True)
    t.start()

    last_song = read_song_file() if watch_song else None
    reason = "eof"

    try:
        while t.is_alive():
            # 100ms poll: boundary/EOF detection latency. The pacer keeps the
            # pipe fed regardless, so this only affects cut timing precision.
            time.sleep(0.1)
            if _pipe_lost.is_set():
                reason = "pipe_lost"
                break
            if _shutdown.is_set():
                reason = "interrupted"
                break
            elapsed = time.monotonic() - start_mono

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
                        break
                else:
                    # Reactive fallback: notice the song already changed.
                    current_song = read_song_file()
                    if current_song != last_song and elapsed >= dwell_sec:
                        reason = "boundary"
                        break
                    # Update last_song even if dwell not reached, so the *next*
                    # eligible check catches the boundary that's already been crossed.
                    if current_song != last_song:
                        last_song = current_song
    finally:
        # SIGKILL, not terminate: a disposable mid-piece decoder needs no
        # graceful flush, and ffmpeg under -re can sit ~5s ignoring SIGTERM
        # (blocked between paced frames) — that delay pushed every boundary
        # cut 5s late, missing the song boundary it was scheduled for.
        stop_event.set()
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        proc.wait()
        t.join(timeout=5)

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
    print(f"[video] pipe open, starting pacer + playback", flush=True)

    pacer = threading.Thread(target=pacer_loop, args=(pipe_fd,), daemon=True)
    pacer.start()

    order    = make_shuffled_order(pieces)
    idx      = 0
    last_id  = None

    while not _shutdown.is_set():
        if idx >= len(order):
            order = make_shuffled_order(pieces, last_id=last_id)
            idx   = 0

        entry  = pieces[order[idx]]
        # Pre-pick the transition ident: the predictive scheduler needs its
        # duration to start the cut early enough for the ident to straddle the
        # song boundary.
        next_ident = pick_ident(idents)
        next_ident_dur = float(next_ident["duration_sec"]) if next_ident else 0.0
        reason, _ = play_entry(entry, watch_song=True, dwell_sec=DWELL_SEC,
                               ident_dur=next_ident_dur)

        if _shutdown.is_set():
            break

        if reason == "pipe_lost":
            # Reader (master ffmpeg) died. The pacer already exited; reopen the
            # pipe (blocks until a new reader appears) and start a fresh pacer.
            print(f"[video] pipe_lost — closing fd and waiting for new reader on "
                  f"{VIDEO_PIPE} ...", flush=True)
            try:
                os.close(pipe_fd)
            except OSError:
                pass
            pacer.join(timeout=5)
            _latest_frame[0] = _BLACK_FRAME
            pipe_fd = os.open(VIDEO_PIPE, os.O_WRONLY)
            _pipe_lost.clear()
            pacer = threading.Thread(target=pacer_loop, args=(pipe_fd,), daemon=True)
            pacer.start()
            print("[video] pipe reopened, resuming playback (replaying current piece)",
                  flush=True)
            # Do NOT advance idx — replay the same piece from the beginning.
            continue

        last_id = entry["id"]
        idx    += 1

        if reason in ("boundary", "eof"):
            # Every piece transition gets an ident — it's what makes a cut read
            # as intentional. Play the PRE-PICKED ident: the scheduler timed the
            # cut so this exact ident's duration lands the next piece on the
            # song boundary.
            if next_ident:
                play_entry(next_ident, watch_song=False)

    try:
        os.close(pipe_fd)
    except OSError:
        pass
    print("[video] shutdown complete", flush=True)


if __name__ == "__main__":
    main()
