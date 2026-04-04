#!/bin/bash
set -e

YOUTUBE_URL="rtmp://a.rtmp.youtube.com/live2"
RESOLUTION="1920x1080"
OUTPUT_RES="1280x720"
FPS="30"

if [ -z "$YOUTUBE_STREAM_KEY" ]; then
    echo "ERROR: YOUTUBE_STREAM_KEY environment variable not set"
    exit 1
fi

# Start virtual framebuffer with no cursor
Xvfb :99 -screen 0 ${RESOLUTION}x24 -nocursor &
export DISPLAY=:99

# Wait for Xvfb to be ready
sleep 2

# Set root window to black and hide cursor
xsetroot -solid black
unclutter -idle 0 -root &

# Start Godot
godot --path /app --main-scene main.tscn --rendering-driver opengl3 &
GODOT_PID=$!

# Wait for Godot to initialize and start rendering
sleep 5

# Start FFmpeg to capture the virtual display and stream to YouTube
ffmpeg \
    -f x11grab \
    -video_size ${RESOLUTION} \
    -framerate ${FPS} \
    -i :99 \
    -f lavfi \
    -i anullsrc=channel_layout=stereo:sample_rate=44100 \
    -vf scale=${OUTPUT_RES} \
    -c:v libx264 \
    -preset ultrafast \
    -tune zerolatency \
    -b:v 2500k \
    -maxrate 2500k \
    -bufsize 5000k \
    -pix_fmt yuv420p \
    -g 60 \
    -c:a aac \
    -b:a 128k \
    -ar 44100 \
    -f flv \
    "${YOUTUBE_URL}/${YOUTUBE_STREAM_KEY}" &
FFMPEG_PID=$!

# If either process dies, kill the other and exit
trap "kill $GODOT_PID $FFMPEG_PID 2>/dev/null; exit" SIGTERM SIGINT

while kill -0 $GODOT_PID 2>/dev/null && kill -0 $FFMPEG_PID 2>/dev/null; do
    sleep 5
done

echo "Process exited, shutting down..."
kill $GODOT_PID $FFMPEG_PID 2>/dev/null
exit 1
