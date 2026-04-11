#!/bin/bash
# Local debug helper for the Godot scene.
#
# Usage:
#   ./debug.sh start           # launch Godot, record PID
#   ./debug.sh stop            # kill the running instance
#   ./debug.sh restart         # stop + start
#   ./debug.sh title "Some Song Title"
#                              # write /tmp/current_song.txt so song_display picks it up
#   ./debug.sh status          # is it running?

set -e

PID_FILE="/tmp/galton-godot.pid"
SONG_FILE="/tmp/current_song.txt"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

is_running() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

start() {
    if is_running; then
        echo "Already running (PID $(cat "$PID_FILE"))"
        return 0
    fi
    cd "$PROJECT_DIR"
    godot --path . --main-scene main.tscn --rendering-driver opengl3 &
    echo $! > "$PID_FILE"
    echo "Started Godot (PID $!)"
}

stop() {
    if ! is_running; then
        echo "Not running"
        rm -f "$PID_FILE"
        return 0
    fi
    local pid
    pid=$(cat "$PID_FILE")
    kill "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.2
    done
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "Stopped"
}

title() {
    if [ -z "$1" ]; then
        echo "Usage: $0 title \"Song Title\"" >&2
        exit 1
    fi
    printf '%s' "$1" > "${SONG_FILE}.tmp"
    mv "${SONG_FILE}.tmp" "$SONG_FILE"
    echo "Wrote '$1' to $SONG_FILE"
}

status() {
    if is_running; then
        echo "Running (PID $(cat "$PID_FILE"))"
    else
        echo "Not running"
    fi
}

case "${1:-}" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; start ;;
    title)   shift; title "$*" ;;
    status)  status ;;
    *)
        echo "Usage: $0 {start|stop|restart|title \"...\"|status}" >&2
        exit 1
        ;;
esac
