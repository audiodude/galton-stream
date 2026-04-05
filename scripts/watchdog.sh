#!/bin/bash
# Monitors FFmpeg streaming process health and alerts via Telegram if the stream stalls.
# Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars.

CHAT_ID="${TELEGRAM_CHAT_ID:?ERROR: TELEGRAM_CHAT_ID not set}"
BOT_TOKEN="${TELEGRAM_BOT_TOKEN:?ERROR: TELEGRAM_BOT_TOKEN not set}"
CHECK_INTERVAL=60
STALL_THRESHOLD=3
PREFIX="Galton monitor:"

stall_count=0
alerted=false

send_telegram() {
    local response
    response=$(curl -s -w "\n%{http_code}" -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        -d chat_id="$CHAT_ID" \
        -d text="$1" 2>&1)
    local http_code=$(echo "$response" | tail -1)
    echo "Telegram send (HTTP $http_code): $1" >&2
}

send_telegram "${PREFIX} Watchdog started, monitoring stream."

while true; do
    sleep "$CHECK_INTERVAL"

    # Find the streaming FFmpeg (the one writing to rtmp), not the decoder instances
    FFMPEG_PID=$(pgrep -f "flv.*rtmp" 2>/dev/null | head -1)

    if [ -z "$FFMPEG_PID" ]; then
        if [ "$alerted" = false ]; then
            send_telegram "${PREFIX} FFmpeg streaming process not found! Stream is down."
            alerted=true
        fi
        continue
    fi

    # Check total network TX bytes (container-level)
    bytes_now=$(awk '/eth0|ens|veth/{s+=$10} END {print s+0}' /proc/net/dev 2>/dev/null)

    if [ -n "$prev_bytes" ] && [ "$bytes_now" = "$prev_bytes" ]; then
        stall_count=$((stall_count + 1))
        echo "Stall check: no new bytes ($stall_count/$STALL_THRESHOLD)" >&2
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
