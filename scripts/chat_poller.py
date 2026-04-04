#!/usr/bin/env python3
"""Polls YouTube Live Chat API using OAuth. Auto-finds active broadcast."""

import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

from chat_events import write_events

TOKEN_FILE = os.environ.get("YOUTUBE_TOKEN_FILE", "youtube_token.json")
POLL_INTERVAL = 5


def load_token_config():
    if not os.path.exists(TOKEN_FILE):
        print(f"ERROR: {TOKEN_FILE} not found. Run youtube_auth.py first.", file=sys.stderr)
        sys.exit(1)
    with open(TOKEN_FILE) as f:
        return json.load(f)


def get_access_token(config):
    """Use refresh token to get a fresh access token."""
    data = urllib.parse.urlencode({
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "refresh_token": config["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()

    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    with urllib.request.urlopen(req) as resp:
        tokens = json.loads(resp.read())
    return tokens["access_token"]


def api_get(url, access_token):
    """Make an authenticated GET request."""
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {access_token}")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def find_active_broadcast(access_token):
    """Find the active live broadcast and return its chat ID."""
    url = (
        "https://www.googleapis.com/youtube/v3/liveBroadcasts"
        "?part=snippet,contentDetails&broadcastStatus=active&broadcastType=all"
    )
    data = api_get(url, access_token)
    items = data.get("items", [])
    if not items:
        return None, None

    broadcast = items[0]
    title = broadcast["snippet"]["title"]
    chat_id = broadcast["snippet"]["liveChatId"]
    return title, chat_id


def run():
    config = load_token_config()
    access_token = get_access_token(config)
    token_refreshed_at = time.time()

    print("Looking for active broadcast...", flush=True)

    chat_id = None
    while not chat_id:
        title, chat_id = find_active_broadcast(access_token)
        if not chat_id:
            print("No active broadcast found. Retrying in 30s...", flush=True)
            time.sleep(30)
            # Refresh token if it's been a while
            if time.time() - token_refreshed_at > 3000:
                access_token = get_access_token(config)
                token_refreshed_at = time.time()

    print(f"Found broadcast: {title}", flush=True)
    print(f"Chat ID: {chat_id}", flush=True)

    page_token = None
    poll_ms = POLL_INTERVAL * 1000
    # Skip initial batch (history) by doing one fetch and ignoring results
    first_poll = True
    seen_users = set()  # Track users we've already welcomed

    while True:
        # Refresh access token every 45 minutes
        if time.time() - token_refreshed_at > 2700:
            try:
                access_token = get_access_token(config)
                token_refreshed_at = time.time()
            except Exception as e:
                print(f"Token refresh failed: {e}", file=sys.stderr, flush=True)

        url = (
            f"https://www.googleapis.com/youtube/v3/liveChat/messages"
            f"?liveChatId={chat_id}&part=snippet,authorDetails"
        )
        if page_token:
            url += f"&pageToken={page_token}"

        try:
            data = api_get(url, access_token)
        except urllib.error.HTTPError as e:
            print(f"API error: {e.code}", file=sys.stderr, flush=True)
            if e.code == 401:
                access_token = get_access_token(config)
                token_refreshed_at = time.time()
            time.sleep(poll_ms / 1000)
            continue
        except (urllib.error.URLError, OSError) as e:
            print(f"Network error: {e}, retrying...", file=sys.stderr, flush=True)
            time.sleep(poll_ms / 1000)
            continue

        page_token = data.get("nextPageToken")
        poll_ms = data.get("pollingIntervalMillis", POLL_INTERVAL * 1000)

        if first_poll:
            first_poll = False
            # Remember existing chatters so we don't welcome them again
            for item in data.get("items", []):
                author = item.get("authorDetails", {})
                seen_users.add(author.get("channelId", ""))
            print(f"Skipped {len(data.get('items', []))} historical messages, "
                  f"{len(seen_users)} known users", flush=True)
            time.sleep(poll_ms / 1000)
            continue

        events = []
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            author = item.get("authorDetails", {})
            msg_type = snippet.get("type", "")
            name = author.get("displayName", "Unknown")
            channel_id = author.get("channelId", "")

            # First message from a user = welcome them
            if channel_id and channel_id not in seen_users:
                seen_users.add(channel_id)
                events.append({
                    "type": "join",
                    "name": name,
                    "time": time.time(),
                })

            if msg_type == "superChatEvent":
                events.append({
                    "type": "gift",
                    "name": name,
                    "amount": snippet.get("superChatDetails", {}).get("amountDisplayString", ""),
                    "time": time.time(),
                })
            elif msg_type == "superStickerEvent":
                events.append({
                    "type": "sticker",
                    "name": name,
                    "amount": snippet.get("superStickerDetails", {}).get("amountDisplayString", ""),
                    "time": time.time(),
                })

        if events:
            write_events(events)
            for e in events:
                print(f"  {e['type']}: {e['name']}", flush=True)

        time.sleep(poll_ms / 1000)


if __name__ == "__main__":
    run()
