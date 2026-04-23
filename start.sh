#!/bin/bash
set -e

YOUTUBE_URL="${YOUTUBE_URL:-rtmp://a.rtmp.youtube.com/live2}"
RESOLUTION="1920x1080"
OUTPUT_RES="1280x720"
FPS="30"
MUSIC_DIR="/data/mp3"
AUDIO_PIPE="/tmp/audio_pipe"
S3_BUCKET="${S3_MUSIC_BUCKET:?ERROR: S3_MUSIC_BUCKET environment variable not set}"
YOUTUBE_STREAM_KEY="${YOUTUBE_STREAM_KEY:-test}"

# Active window — galton-stream only runs the Godot/ffmpeg/chat/music
# stack between these hours in America/Los_Angeles. Outside the window
# start.sh sleeps and no broadcast exists: galton-monitor creates the
# day's YouTube broadcast at window open and tears it down (transitions
# to complete, becomes a private VOD) at window close.
ACTIVE_START_HOUR=12
ACTIVE_END_HOUR=18

in_active_window() {
    local h
    h=$(TZ=America/Los_Angeles date +%-H)
    [ "$h" -ge "$ACTIVE_START_HOUR" ] && [ "$h" -lt "$ACTIVE_END_HOUR" ]
}

# Sync music from S3 if the folder is empty (do this even outside the
# window so we're ready the moment it opens).
mkdir -p "$MUSIC_DIR"
if [ -z "$(ls -A $MUSIC_DIR/*.mp3 2>/dev/null)" ]; then
    echo "Syncing music from S3..."
    aws s3 sync "$S3_BUCKET" "$MUSIC_DIR/" --no-progress
    echo "Music sync complete: $(ls $MUSIC_DIR/*.mp3 | wc -l) tracks"
else
    echo "Music already present: $(ls $MUSIC_DIR/*.mp3 | wc -l) tracks"
fi

# Wait until we're inside the active window.
while ! in_active_window; do
    now=$(TZ=America/Los_Angeles date '+%H:%M %Z')
    echo "Outside active window (${ACTIVE_START_HOUR}:00-${ACTIVE_END_HOUR}:00 PT), now $now; sleeping 60s..."
    sleep 60
done
echo "Inside active window, starting stream components..."

# Clean up stale X lock from previous crash
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

# Start virtual framebuffer with no cursor
Xvfb :99 -screen 0 ${RESOLUTION}x24 -nocursor &
export DISPLAY=:99

# Wait for Xvfb to be ready
sleep 2

# Set root window to black and hide cursor
xsetroot -solid black
unclutter -idle 0 -root &

# Start music player (decodes audio to pipe, writes playlist state)
MUSIC_DIR="$MUSIC_DIR" python3 /app/scripts/music_player.py &
MUSIC_PID=$!

# Start title writer (reads playlist state, writes song title on wall-clock schedule)
python3 /app/scripts/title_writer.py &
TITLE_PID=$!
echo $TITLE_PID > /tmp/title_writer.pid

# Start YouTube chat poller (writes events to /tmp/chat_events.json for Godot)
python3 /app/scripts/chat_poller.py &
CHAT_PID=$!
echo $CHAT_PID > /tmp/chat_poller.pid

# Wait for pipe to be created
sleep 2

# CPU pinning: split cores between Godot (physics/render) and ffmpeg (encode)
# so the encoder can't be starved by a Godot spike. Falls back to no pinning
# if fewer than 4 cores are visible.
NCORES=$(nproc)
if [ "$NCORES" -ge 4 ]; then
    HALF=$((NCORES / 2))
    GODOT_CPUS="0-$((HALF - 1))"
    FFMPEG_CPUS="$HALF-$((NCORES - 1))"
    # libx264 warns against >16 threads; cap here even if more cores are pinned.
    FFMPEG_THREADS=$((NCORES - HALF))
    if [ "$FFMPEG_THREADS" -gt 16 ]; then
        FFMPEG_THREADS=16
    fi
    GODOT_TASKSET="taskset -c $GODOT_CPUS"
    FFMPEG_TASKSET="taskset -c $FFMPEG_CPUS"
    echo "CPU pinning: Godot -> $GODOT_CPUS, ffmpeg -> $FFMPEG_CPUS ($FFMPEG_THREADS threads)"
else
    GODOT_TASKSET=""
    FFMPEG_TASKSET=""
    FFMPEG_THREADS=0  # libx264 auto
    echo "CPU pinning: skipped (only $NCORES cores visible)"
fi

# Start Godot
$GODOT_TASKSET godot --path /app --main-scene main.tscn --rendering-driver opengl3 &
GODOT_PID=$!

# Wait for Godot to initialize and start rendering
sleep 5

# Start FFmpeg in a retry loop — if YouTube's RTMP drops, restart FFmpeg
# instead of tearing down the whole container. music_player already handles
# the reader-died case by reopening the pipe.
(
    while true; do
        $FFMPEG_TASKSET ffmpeg \
            -loglevel warning \
            -stats_period 5 \
            -thread_queue_size 256 \
            -f x11grab \
            -video_size ${RESOLUTION} \
            -framerate ${FPS} \
            -i :99 \
            -thread_queue_size 8 \
            -re \
            -f s16le \
            -ar 44100 \
            -ac 2 \
            -i "$AUDIO_PIPE" \
            -vf scale=${OUTPUT_RES} \
            -af volume=-7dB \
            -c:v libx264 \
            -preset ultrafast \
            -tune zerolatency \
            -threads ${FFMPEG_THREADS} \
            -b:v 2500k \
            -maxrate 2500k \
            -bufsize 7500k \
            -pix_fmt yuv420p \
            -g 60 \
            -c:a aac \
            -b:a 128k \
            -ar 44100 \
            -f flv \
            "${YOUTUBE_URL}/${YOUTUBE_STREAM_KEY}" || true
        echo "FFmpeg exited, restarting in 3s..."
        sleep 3
    done
) &
FFMPEG_PID=$!

# Start health server (replaces watchdog.sh — exposes /health for galton-monitor)
python3 /app/scripts/health_server.py &
HEALTH_PID=$!

# If any process dies, kill the others and exit
trap "kill $GODOT_PID $FFMPEG_PID $MUSIC_PID $TITLE_PID $CHAT_PID $HEALTH_PID 2>/dev/null; exit" SIGTERM SIGINT

while kill -0 $GODOT_PID 2>/dev/null && kill -0 $FFMPEG_PID 2>/dev/null && kill -0 $MUSIC_PID 2>/dev/null; do
    if ! in_active_window; then
        echo "Active window closed, tearing down stream components..."
        break
    fi
    sleep 5
done

echo "Shutting down..."
kill $GODOT_PID $FFMPEG_PID $MUSIC_PID $TITLE_PID $CHAT_PID $HEALTH_PID 2>/dev/null
exit 1
