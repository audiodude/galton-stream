#!/usr/bin/env bash
# Live playout: stream-copy the day's pre-baked show to YouTube RTMP.
# Env: SHOWS_DIR (/data/shows), YOUTUBE_URL, YOUTUBE_STREAM_KEY, PLAYOUT_HEARTBEAT.
set -uo pipefail
SHOWS_DIR="${SHOWS_DIR:-/data/shows}"
YOUTUBE_URL="${YOUTUBE_URL:-rtmp://a.rtmp.youtube.com/live2}"
HEARTBEAT="${PLAYOUT_HEARTBEAT:-/tmp/playout_heartbeat}"

resolve_show() {
    # today's LATEST if present and valid, else the newest show-*.mkv on disk.
    local latest="$SHOWS_DIR/LATEST"
    if [[ -f "$latest" ]] && [[ -f "$SHOWS_DIR/$(cat "$latest")" ]]; then
        echo "$SHOWS_DIR/$(cat "$latest")"; return
    fi
    ls -t "$SHOWS_DIR"/show-*.mkv 2>/dev/null | head -1
}

FFPID=""
HB=""

cleanup() {
    # Forward SIGTERM to ffmpeg (in its own session) and wait for a clean flush.
    [[ -n "${FFPID:-}" ]] && kill -TERM "$FFPID" 2>/dev/null && wait "$FFPID" 2>/dev/null || true
    [[ -n "${HB:-}" ]] && kill "$HB" 2>/dev/null || true
    exit 0
}
trap cleanup SIGTERM SIGINT

while true; do
    SHOW="$(resolve_show)"
    if [[ -z "$SHOW" || ! -f "$SHOW" ]]; then
        echo "[playout] no show available; retrying in 15s" >&2; sleep 15; continue
    fi
    echo "[playout] streaming $SHOW"
    # touch heartbeat every 5s in the background while ffmpeg runs
    ( while :; do touch "$HEARTBEAT"; sleep 5; done ) & HB=$!
    # setsid: run ffmpeg in a new session so a process-group SIGTERM (e.g. from `timeout`)
    # doesn't race with our own cleanup trap—only our trap sends SIGTERM to ffmpeg.
    setsid ffmpeg -hide_banner -loglevel warning -re -i "$SHOW" \
        -c copy -f flv "$YOUTUBE_URL/${YOUTUBE_STREAM_KEY:?}" &
    FFPID=$!
    wait "$FFPID" || true
    FFPID=""
    kill "$HB" 2>/dev/null || true
    HB=""
    echo "[playout] ffmpeg exited; restart in 3s" >&2
    sleep 3
done
