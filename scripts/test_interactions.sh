#!/bin/bash
# Quick local test: launches Godot with short rounds and a fast mock chat server.
# Usage: ./scripts/test_interactions.sh

cd "$(dirname "$0")/.."

kill $(pgrep -x godot) 2>/dev/null
sleep 1

BALLS_PER_ROUND=80 godot --path . --main-scene main.tscn &
GODOT_PID=$!
sleep 3

python3 scripts/mock_youtube.py \
    --join-delay-ms 4000 \
    --welcome-back-delay-ms 6000 \
    --gift-delay-ms 8000 \
    --sticker-delay-ms 10000 &
MOCK_PID=$!

trap "kill $GODOT_PID $MOCK_PID 2>/dev/null; exit" SIGINT SIGTERM

echo "Running: Godot (PID $GODOT_PID) + Mock chat (PID $MOCK_PID)"
echo "Press Ctrl+C to stop"
wait $GODOT_PID
kill $MOCK_PID 2>/dev/null
