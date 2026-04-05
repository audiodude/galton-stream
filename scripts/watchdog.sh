#!/bin/bash
# Monitors FFmpeg streaming process health and alerts via Telegram if the stream stalls.
# Falls back to streaming a static backup image if the main stream dies.
# Requires TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, and YOUTUBE_STREAM_KEY env vars.

CHAT_ID="${TELEGRAM_CHAT_ID:?ERROR: TELEGRAM_CHAT_ID not set}"
BOT_TOKEN="${TELEGRAM_BOT_TOKEN:?ERROR: TELEGRAM_BOT_TOKEN not set}"
YOUTUBE_URL="rtmp://a.rtmp.youtube.com/live2"
BACKUP_IMAGE="/app/assets/backup.png"
CHECK_INTERVAL=60
STALL_THRESHOLD=3
PREFIX="Galton monitor:"

stall_count=0
alerted=false
fallback_pid=""

send_telegram() {
    local response
    response=$(curl -s -w "\n%{http_code}" -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        -d chat_id="$CHAT_ID" \
        -d text="$1" > /dev/null 2>&1)
    echo "Telegram: $1" >&2
}

start_fallback() {
    # Kill any existing fallback
    if [ -n "$fallback_pid" ] && kill -0 "$fallback_pid" 2>/dev/null; then
        return
    fi

    if [ ! -f "$BACKUP_IMAGE" ] || [ -z "$YOUTUBE_STREAM_KEY" ]; then
        echo "Cannot start fallback: missing image or stream key" >&2
        return
    fi

    send_telegram "${PREFIX} Starting fallback stream (backup image)."

    ffmpeg -loglevel warning \
        -loop 1 -re -i "$BACKUP_IMAGE" \
        -f lavfi -i anullsrc=r=44100:cl=stereo \
        -c:v libx264 -preset ultrafast -tune stillimage \
        -b:v 500k -maxrate 500k -bufsize 1000k \
        -pix_fmt yuv420p -g 60 \
        -c:a aac -b:a 128k -ar 44100 \
        -shortest -f flv \
        "${YOUTUBE_URL}/${YOUTUBE_STREAM_KEY}" &
    fallback_pid=$!
    echo "Fallback stream started (PID $fallback_pid)" >&2
}

stop_fallback() {
    if [ -n "$fallback_pid" ] && kill -0 "$fallback_pid" 2>/dev/null; then
        kill "$fallback_pid" 2>/dev/null
        wait "$fallback_pid" 2>/dev/null
        fallback_pid=""
        send_telegram "${PREFIX} Fallback stream stopped, main stream recovered."
    fi
}

send_telegram "${PREFIX} Watchdog started, monitoring stream."

while true; do
    sleep "$CHECK_INTERVAL"

    # Find the main streaming FFmpeg (the one writing to rtmp), not fallback or decoders
    FFMPEG_PID=$(pgrep -f "flv.*rtmp" 2>/dev/null | grep -v "^${fallback_pid}$" | head -1)

    if [ -z "$FFMPEG_PID" ]; then
        if [ "$alerted" = false ]; then
            send_telegram "${PREFIX} Main FFmpeg process not found! Stream is down."
            alerted=true
            start_fallback
        fi
        continue
    fi

    # Check total network TX bytes (container-level)
    bytes_now=$(awk '/eth0|ens|veth/{s+=$10} END {print s+0}' /proc/net/dev 2>/dev/null)

    if [ -n "$prev_bytes" ] && [ "$bytes_now" = "$prev_bytes" ]; then
        stall_count=$((stall_count + 1))
        echo "Stall check: no new bytes ($stall_count/$STALL_THRESHOLD)" >&2
        if [ "$stall_count" -ge "$STALL_THRESHOLD" ] && [ "$alerted" = false ]; then
            send_telegram "${PREFIX} Stream stalled — no bytes sent in $((STALL_THRESHOLD * CHECK_INTERVAL))s."
            alerted=true
            # Kill the hung main FFmpeg and start fallback
            kill "$FFMPEG_PID" 2>/dev/null
            start_fallback
        fi
    else
        if [ "$alerted" = true ]; then
            stop_fallback
            send_telegram "${PREFIX} Stream recovered, bytes flowing again."
        fi
        stall_count=0
        alerted=false
    fi

    prev_bytes="$bytes_now"
done
