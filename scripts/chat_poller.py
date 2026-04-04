#!/usr/bin/env python3
"""Polls YouTube Live Chat API and writes events for Godot to read."""

import json
import os
import sys
import time

from chat_events import write_events

POLL_INTERVAL = 5


def run(api_key, video_id):
    import urllib.request
    import urllib.error

    # Get the live chat ID from the video
    url = (
        f"https://www.googleapis.com/youtube/v3/videos"
        f"?part=liveStreamingDetails&id={video_id}&key={api_key}"
    )
    try:
        with urllib.request.urlopen(url) as resp:
            data = json.loads(resp.read())
        chat_id = data["items"][0]["liveStreamingDetails"]["activeLiveChatId"]
        print(f"Live chat ID: {chat_id}", flush=True)
    except (KeyError, IndexError):
        print("ERROR: Could not find live chat for this video. Is it streaming?",
              file=sys.stderr)
        sys.exit(1)

    page_token = None
    poll_ms = POLL_INTERVAL * 1000

    while True:
        url = (
            f"https://www.googleapis.com/youtube/v3/liveChat/messages"
            f"?liveChatId={chat_id}&part=snippet,authorDetails&key={api_key}"
        )
        if page_token:
            url += f"&pageToken={page_token}"

        try:
            with urllib.request.urlopen(url) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            print(f"API error: {e.code}", file=sys.stderr, flush=True)
            time.sleep(poll_ms / 1000)
            continue

        page_token = data.get("nextPageToken")
        poll_ms = data.get("pollingIntervalMillis", POLL_INTERVAL * 1000)

        events = []
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            author = item.get("authorDetails", {})
            msg_type = snippet.get("type", "")
            name = author.get("displayName", "Unknown")

            if msg_type == "textMessageEvent":
                events.append({
                    "type": "message",
                    "name": name,
                    "text": snippet.get("textMessageDetails", {}).get("messageText", ""),
                    "time": time.time(),
                })
            elif msg_type == "superChatEvent":
                events.append({
                    "type": "gift",
                    "name": name,
                    "amount": snippet.get("superChatDetails", {}).get("amountDisplayString", ""),
                    "time": time.time(),
                })
            elif msg_type == "newSponsorEvent":
                events.append({
                    "type": "join",
                    "name": name,
                    "time": time.time(),
                })

        write_events(events)
        if events:
            for e in events:
                print(f"  {e['type']}: {e['name']}", flush=True)

        time.sleep(poll_ms / 1000)


if __name__ == "__main__":
    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    video_id = os.environ.get("YOUTUBE_VIDEO_ID", "")
    if not api_key or not video_id:
        print("ERROR: Set YOUTUBE_API_KEY and YOUTUBE_VIDEO_ID env vars", file=sys.stderr)
        sys.exit(1)
    run(api_key, video_id)
