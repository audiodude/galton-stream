#!/usr/bin/env bash
# playout.sh — Master playout orchestrator (test/production mode).
#
# PREREQUISITES (must already be running before this script starts):
#   - music_player.py  → writing raw PCM to $AUDIO_PIPE
#   - title_writer.py  → writing /tmp/current_song.txt (or any process that
#                        keeps SONG_FILE updated with the current song name)
#
# Startup order matters: pipe WRITERS must exist before ffmpeg (the reader)
# opens the pipes, otherwise ffmpeg will error with "No such file or directory"
# (named pipes block on open until both ends are open).
# video_player.py is started here and blocks internally until ffmpeg opens
# the read end of VIDEO_PIPE — this is expected and correct.
#
# ENV:
#   CATALOG_DIR         default: ./catalog
#   AUDIO_PIPE          default: /tmp/audio_pipe
#   VIDEO_PIPE          default: /tmp/video_pipe
#   SONG_FILE           default: /tmp/current_song.txt
#   DWELL_SEC           default: 900  (pass 90 for quick testing)
#   OUTPUT              default: ./playout_test.flv
#                       If YOUTUBE_STREAM_KEY is set, output goes to RTMP.
#   YOUTUBE_URL         default: rtmp://a.rtmp.youtube.com/live2

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: playout.sh
       (no arguments — configured entirely via environment variables)

Streams the pre-rendered video catalog muxed with the live music pipe to a
local FLV file or YouTube RTMP, cutting between pieces at song boundaries.

PREREQUISITES (must already be running):
  music_player.py   writing raw PCM to $AUDIO_PIPE
  title_writer.py   keeping $SONG_FILE updated with the current song name

ENVIRONMENT:
  CATALOG_DIR         dir with mp4s + catalog.json     (default: ./catalog)
  AUDIO_PIPE          music input pipe                 (default: /tmp/audio_pipe)
  VIDEO_PIPE          video input pipe                 (default: /tmp/video_pipe)
  SONG_FILE           song-boundary + overlay source   (default: /tmp/current_song.txt)
  DWELL_SEC           min piece play time before a cut (default: 900; use 90 for tests)
  OUTPUT              .flv path, .mp4 path, or rtmp:// URL (default: ./playout_test.flv)
                      .mp4 records to a kill-safe companion .flv, then losslessly
                      remuxes to the .mp4 on shutdown
  YOUTUBE_STREAM_KEY  if set, OUTPUT defaults to YouTube RTMP
  YOUTUBE_URL         RTMP base (default: rtmp://a.rtmp.youtube.com/live2)

EXAMPLES:
  DWELL_SEC=90 CATALOG_DIR=./catalog scripts/playout/playout.sh
  YOUTUBE_STREAM_KEY=xxxx-xxxx scripts/playout/playout.sh
EOF
}

if [ "$#" -gt 0 ]; then
    case "$1" in
        -h|--help) usage; exit 0 ;;
        *) echo "playout.sh takes no arguments (got: $*)" >&2; echo >&2; usage >&2; exit 1 ;;
    esac
fi

CATALOG_DIR="${CATALOG_DIR:-./catalog}"
AUDIO_PIPE="${AUDIO_PIPE:-/tmp/audio_pipe}"
VIDEO_PIPE="${VIDEO_PIPE:-/tmp/video_pipe}"
SONG_FILE="${SONG_FILE:-/tmp/current_song.txt}"
DWELL_SEC="${DWELL_SEC:-900}"
YOUTUBE_URL="${YOUTUBE_URL:-rtmp://a.rtmp.youtube.com/live2}"
FONT="/usr/share/fonts/TTF/DejaVuSans.ttf"

if [ -n "${YOUTUBE_STREAM_KEY:-}" ]; then
    OUTPUT="${OUTPUT:-${YOUTUBE_URL}/${YOUTUBE_STREAM_KEY}}"
else
    OUTPUT="${OUTPUT:-./playout_test.flv}"
fi

# An .mp4 OUTPUT can't be written directly: mp4 needs its index written at
# clean shutdown, and playout gets killed by design — a killed mp4 is garbage.
# So record to a companion .flv (kill-safe) and losslessly remux on exit.
MP4_OUTPUT=""
if [[ "$OUTPUT" == *.mp4 ]]; then
    MP4_OUTPUT="$OUTPUT"
    OUTPUT="${OUTPUT%.mp4}.flv"
    echo "[playout] mp4 requested: recording to $OUTPUT, will remux to $MP4_OUTPUT on exit"
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

VIDEO_PLAYER_PID=""
FFMPEG_PID=""

cleanup() {
    echo "[playout] shutting down..."
    [ -n "$VIDEO_PLAYER_PID" ] && kill "$VIDEO_PLAYER_PID" 2>/dev/null || true
    if [ -n "$FFMPEG_PID" ]; then
        kill "$FFMPEG_PID" 2>/dev/null || true
        wait "$FFMPEG_PID" 2>/dev/null || true
    fi
    # Remove pipes so the next run starts clean.
    rm -f "$VIDEO_PIPE"
    if [ -n "$MP4_OUTPUT" ] && [ -s "$OUTPUT" ]; then
        echo "[playout] remuxing $OUTPUT -> $MP4_OUTPUT"
        if ffmpeg -y -loglevel error -i "$OUTPUT" -c copy "$MP4_OUTPUT"; then
            rm -f "$OUTPUT"
            echo "[playout] wrote $MP4_OUTPUT"
        else
            echo "[playout] remux failed — recording kept at $OUTPUT" >&2
        fi
    fi
    echo "[playout] done"
}
trap cleanup EXIT INT TERM

echo "[playout] CATALOG_DIR=$CATALOG_DIR"
echo "[playout] AUDIO_PIPE=$AUDIO_PIPE  VIDEO_PIPE=$VIDEO_PIPE"
echo "[playout] DWELL_SEC=$DWELL_SEC  OUTPUT=$OUTPUT"

# Ensure SONG_FILE exists so ffmpeg's drawtext filter can initialize even if
# title_writer.py hasn't written it yet (e.g. on first startup before
# music_player decodes a song and writes the state file).
if [ ! -f "$SONG_FILE" ]; then
    echo "" > "$SONG_FILE"
    echo "[playout] created empty $SONG_FILE"
fi

# Start video_player in the background. It will create VIDEO_PIPE itself and
# block internally until ffmpeg opens the read end — that ordering is safe
# because mkfifo happens before the write-end open.
CATALOG_DIR="$CATALOG_DIR" \
VIDEO_PIPE="$VIDEO_PIPE" \
SONG_FILE="$SONG_FILE" \
DWELL_SEC="$DWELL_SEC" \
    python3 "$HERE/video_player.py" &
VIDEO_PLAYER_PID=$!
echo "[playout] video_player pid=$VIDEO_PLAYER_PID"

# Give video_player a moment to call mkfifo before ffmpeg tries to open the pipe.
sleep 1

echo "[playout] starting master ffmpeg → $OUTPUT"

# Encoder settings for 720p60:
#   video: libx264 veryfast, 4500k (up from start.sh's 2500k@30fps), keyframe
#          every 60 frames (1s at 60fps), yuv420p.
#   audio: aac 128k 44.1kHz stereo (same as start.sh).
#   drawtext: textfile reload=1 so ffmpeg re-reads current_song.txt each frame.
#   format: flv works for both local file and RTMP targets.
ffmpeg \
    -loglevel warning \
    -stats_period 5 \
    -thread_queue_size 256 \
    -f rawvideo \
    -pix_fmt yuv420p \
    -s 1280x720 \
    -r 60 \
    -i "$VIDEO_PIPE" \
    -thread_queue_size 8 \
    -f s16le \
    -ar 44100 \
    -ac 2 \
    -i "$AUDIO_PIPE" \
    -vf "drawtext=fontfile=${FONT}:textfile=${SONG_FILE}:reload=1:\
fontsize=28:fontcolor=white:shadowcolor=black:shadowx=2:shadowy=2:\
x=20:y=h-th-20" \
    -c:v libx264 \
    -preset veryfast \
    -tune zerolatency \
    -b:v 4500k \
    -maxrate 4500k \
    -bufsize 9000k \
    -pix_fmt yuv420p \
    -g 60 \
    -c:a aac \
    -b:a 128k \
    -ar 44100 \
    -f flv \
    "$OUTPUT" &
FFMPEG_PID=$!
echo "[playout] ffmpeg pid=$FFMPEG_PID"

wait "$FFMPEG_PID"
echo "[playout] ffmpeg exited"
