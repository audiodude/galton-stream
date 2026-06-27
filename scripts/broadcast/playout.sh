#!/usr/bin/env bash
# Live playout: stream-copy the day's pre-baked show to YouTube RTMP, GATED to the
# operational window (11:45-18:05 PT — see window.py / monitor/windows.py).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SHOWS_DIR="${SHOWS_DIR:-/data/shows}"
YOUTUBE_URL="${YOUTUBE_URL:-rtmp://a.rtmp.youtube.com/live2}"
HEARTBEAT="${PLAYOUT_HEARTBEAT:-/tmp/playout_heartbeat}"
WINDOW_CMD="${WINDOW_CMD:-python3 $HERE/window.py}"   # exit 0 == in operational window
FFMPEG_BIN="${FFMPEG_BIN:-ffmpeg}"
SUPERVISE_INTERVAL="${SUPERVISE_INTERVAL:-15}"

resolve_show() {
    local latest="$SHOWS_DIR/LATEST"
    if [[ -f "$latest" ]] && [[ -f "$SHOWS_DIR/$(cat "$latest")" ]]; then
        echo "$SHOWS_DIR/$(cat "$latest")"; return
    fi
    ls -t "$SHOWS_DIR"/show-*.mkv 2>/dev/null | head -1
}

FFPID=""; HB=""
stop_ff() { [[ -n "${FFPID:-}" ]] && kill -TERM "$FFPID" 2>/dev/null && wait "$FFPID" 2>/dev/null; FFPID=""; }
stop_hb() { [[ -n "${HB:-}" ]] && kill "$HB" 2>/dev/null; HB=""; }
cleanup() { stop_ff; stop_hb; exit 0; }
trap cleanup SIGTERM SIGINT
in_window() { $WINDOW_CMD; }

while true; do
    if ! in_window; then stop_ff; stop_hb; sleep "$SUPERVISE_INTERVAL"; continue; fi
    SHOW="$(resolve_show)"
    if [[ -z "$SHOW" || ! -f "$SHOW" ]]; then
        echo "[playout] no show available; retrying in 15s" >&2; sleep 15; continue
    fi
    echo "[playout] streaming $SHOW" >&2
    ( while :; do touch "$HEARTBEAT"; sleep 5; done ) & HB=$!
    setsid "$FFMPEG_BIN" -hide_banner -loglevel warning -re -i "$SHOW" \
        -c copy -f flv "$YOUTUBE_URL/${YOUTUBE_STREAM_KEY:?}" &
    FFPID=$!
    # supervise: kill ffmpeg if the window closes; otherwise wait for it to exit
    while kill -0 "$FFPID" 2>/dev/null; do
        if ! in_window; then echo "[playout] window closed; stopping ffmpeg" >&2; stop_ff; break; fi
        sleep "$SUPERVISE_INTERVAL"
    done
    if [[ -n "${FFPID:-}" ]]; then   # ffmpeg exited on its own (window still open)
        wait "$FFPID" 2>/dev/null || true; FFPID=""; stop_hb
        echo "[playout] ffmpeg exited; restart in 3s" >&2; sleep 3
    else                              # we killed it on window close
        stop_hb
    fi
done
