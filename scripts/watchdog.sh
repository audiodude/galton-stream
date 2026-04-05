#!/bin/bash
# Monitors FFmpeg process health and alerts via Telegram if the stream stalls.
# Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars.

CHAT_ID="${TELEGRAM_CHAT_ID:?ERROR: TELEGRAM_CHAT_ID not set}"
BOT_TOKEN="${TELEGRAM_BOT_TOKEN:?ERROR: TELEGRAM_BOT_TOKEN not set}"
CHECK_INTERVAL=60
STALL_THRESHOLD=3
PREFIX="Galton monitor:"

stall_count=0
alerted=false

send_telegram() {
    curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        -d chat_id="$CHAT_ID" \
        -d text="$1" > /dev/null 2>&1
}

send_telegram "${PREFIX} Watchdog started, monitoring stream."

while true; do
    sleep "$CHECK_INTERVAL"

    # Check if FFmpeg is running
    if ! pgrep -x ffmpeg > /dev/null 2>&1; then
        if [ "$alerted" = false ]; then
            send_telegram "${PREFIX} FFmpeg process is dead! Stream is down."
            alerted=true
        fi
        continue
    fi

    # Check if FFmpeg is still writing to the RTMP output by looking at /proc net stats
    # Use the ffmpeg pid's fd to check if bytes are being sent
    FFMPEG_PID=$(pgrep -x ffmpeg | tail -1)
    if [ -z "$FFMPEG_PID" ]; then
        continue
    fi

    # Check network bytes sent by this process
    bytes_now=$(awk '{s+=$10} END {print s}' /proc/$FFMPEG_PID/net/dev 2>/dev/null || echo 0)

    if [ -n "$prev_bytes" ] && [ "$bytes_now" = "$prev_bytes" ]; then
        stall_count=$((stall_count + 1))
        if [ "$stall_count" -ge "$STALL_THRESHOLD" ] && [ "$alerted" = false ]; then
            send_telegram "${PREFIX} Stream appears stalled — no bytes sent in $((STALL_THRESHOLD * CHECK_INTERVAL))s."
            alerted=true
        fi
    else
        if [ "$alerted" = true ]; then
            send_telegram "${PREFIX} Stream recovered, bytes flowing again."
        fi
        stall_count=0
        alerted=false
    fi

    prev_bytes="$bytes_now"
done
