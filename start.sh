#!/bin/bash
set -e

YOUTUBE_URL="rtmp://a.rtmp.youtube.com/live2"
RESOLUTION="1920x1080"
OUTPUT_RES="1280x720"
FPS="30"
MUSIC_DIR="/data/mp3"
AUDIO_PIPE="/tmp/audio_pipe"
S3_BUCKET="${S3_MUSIC_BUCKET:?ERROR: S3_MUSIC_BUCKET environment variable not set}"

if [ -z "$YOUTUBE_STREAM_KEY" ]; then
    echo "ERROR: YOUTUBE_STREAM_KEY environment variable not set"
    exit 1
fi

# Sync music from S3 if the folder is empty
mkdir -p "$MUSIC_DIR"
if [ -z "$(ls -A $MUSIC_DIR/*.mp3 2>/dev/null)" ]; then
    echo "Syncing music from S3..."
    aws s3 sync "$S3_BUCKET" "$MUSIC_DIR/" --no-progress
    echo "Music sync complete: $(ls $MUSIC_DIR/*.mp3 | wc -l) tracks"
else
    echo "Music already present: $(ls $MUSIC_DIR/*.mp3 | wc -l) tracks"
fi

# Start virtual framebuffer with no cursor
Xvfb :99 -screen 0 ${RESOLUTION}x24 -nocursor &
export DISPLAY=:99

# Wait for Xvfb to be ready
sleep 2

# Set root window to black and hide cursor
xsetroot -solid black
unclutter -idle 0 -root &

# Start music player (writes current song to /tmp/current_song.txt, audio to pipe)
MUSIC_DIR="$MUSIC_DIR" python3 /app/scripts/music_player.py &
MUSIC_PID=$!

# Wait for pipe to be created
sleep 2

# Start Godot
godot --path /app --main-scene main.tscn --rendering-driver opengl3 &
GODOT_PID=$!

# Wait for Godot to initialize and start rendering
sleep 5

# Start FFmpeg — video from X11, audio from named pipe
ffmpeg \
    -f x11grab \
    -video_size ${RESOLUTION} \
    -framerate ${FPS} \
    -i :99 \
    -f s16le \
    -ar 44100 \
    -ac 2 \
    -i "$AUDIO_PIPE" \
    -vf scale=${OUTPUT_RES} \
    -af volume=-7dB \
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

# If any process dies, kill the others and exit
trap "kill $GODOT_PID $FFMPEG_PID $MUSIC_PID 2>/dev/null; exit" SIGTERM SIGINT

while kill -0 $GODOT_PID 2>/dev/null && kill -0 $FFMPEG_PID 2>/dev/null && kill -0 $MUSIC_PID 2>/dev/null; do
    sleep 5
done

echo "Process exited, shutting down..."
kill $GODOT_PID $FFMPEG_PID $MUSIC_PID 2>/dev/null
exit 1
