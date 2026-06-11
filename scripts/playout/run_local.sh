#!/usr/bin/env bash
# run_local.sh — start the COMPLETE local playout stack in the right order:
# music_player + title_writer + playout (video player + master ffmpeg).
# One command, correct env and absolute paths for every child, one Ctrl-C
# (or SIGTERM) tears it all down. Exists because hand-launching these three
# processes with their cwd/env coupling has proven error-prone.
#
# Usage:
#   scripts/playout/run_local.sh                       # file mode -> ./playout_test.mp4
#   YOUTUBE_STREAM_KEY=... scripts/playout/run_local.sh  # stream to YouTube
#
# ENV (all optional):
#   MUSIC_DIR    default <repo>/music_local
#   STATE_FILE   default /tmp/playout_state.json
#   DWELL_SEC, CATALOG_DIR, OUTPUT, YOUTUBE_STREAM_KEY, YOUTUBE_URL — see playout.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

MUSIC_DIR="${MUSIC_DIR:-$REPO/music_local}"
STATE_FILE="${STATE_FILE:-/tmp/playout_state.json}"

MUSIC_PID=""
TITLE_PID=""

cleanup() {
    echo "[run_local] shutting down music stack..."
    [ -n "$TITLE_PID" ] && kill "$TITLE_PID" 2>/dev/null || true
    [ -n "$MUSIC_PID" ] && kill "$MUSIC_PID" 2>/dev/null || true
    echo "[run_local] done"
}
trap cleanup EXIT INT TERM

MUSIC_DIR="$MUSIC_DIR" STATE_FILE="$STATE_FILE" \
    python3 "$REPO/scripts/music_player.py" &
MUSIC_PID=$!
echo "[run_local] music_player pid=$MUSIC_PID (MUSIC_DIR=$MUSIC_DIR)"

STATE_FILE="$STATE_FILE" \
    python3 "$REPO/scripts/title_writer.py" &
TITLE_PID=$!
echo "[run_local] title_writer pid=$TITLE_PID"

# Sanity: both children must survive startup (a bad path dies instantly).
sleep 2
kill -0 "$MUSIC_PID" 2>/dev/null || { echo "[run_local] music_player died at startup" >&2; exit 1; }
kill -0 "$TITLE_PID" 2>/dev/null || { echo "[run_local] title_writer died at startup" >&2; exit 1; }

# playout.sh runs in the foreground; its own trap handles the video side.
cd "$REPO"
exec_status=0
"$HERE/playout.sh" || exec_status=$?
exit "$exec_status"
