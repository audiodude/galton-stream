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
LIVE_CHAT_ID = os.environ.get("YOUTUBE_LIVE_CHAT_ID", "")
POLL_INTERVAL = 5


def load_token_config():
    # Prefer env vars (same ones galton-monitor uses)
    client_id = os.environ.get("YOUTUBE_CLIENT_ID", "")
    client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
    refresh_token = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")
    if client_id and client_secret and refresh_token:
        print("Using YouTube OAuth from environment variables", flush=True)
        return {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        }
    # Fall back to token file
    if not os.path.exists(TOKEN_FILE):
        print(f"ERROR: No YouTube OAuth credentials. Set YOUTUBE_CLIENT_ID, "
              f"YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN env vars, "
              f"or create {TOKEN_FILE}.", file=sys.stderr)
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
    try:
        data = api_get(url, access_token)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()
        except Exception:
            pass
        print(f"Broadcast API error: {e.code} {body}", file=sys.stderr, flush=True)
        return None, None
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

    if LIVE_CHAT_ID:
        chat_id = LIVE_CHAT_ID
        print(f"Using chat ID from env: {chat_id}", flush=True)
    else:
        print("Looking for active broadcast...", flush=True)
        chat_id = None
        backoff = 30
        while not chat_id:
            title, chat_id = find_active_broadcast(access_token)
            if not chat_id:
                print(f"No active broadcast found. Retrying in {backoff}s...", flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 900)  # max 15 min backoff
                if time.time() - token_refreshed_at > 3000:
                    access_token = get_access_token(config)
                    token_refreshed_at = time.time()
        print(f"Found broadcast: {title}", flush=True)
        print(f"Chat ID: {chat_id}", flush=True)

    page_token = None
    poll_ms = POLL_INTERVAL * 1000
    # Skip initial batch (history) by doing one fetch and ignoring results
    first_poll = True
    seen_users = {}  # channel_id -> last_seen timestamp

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
            body = ""
            try:
                body = e.read().decode()
            except Exception:
                pass
            print(f"API error: {e.code} {body}", file=sys.stderr, flush=True)
            if e.code == 401:
                access_token = get_access_token(config)
                token_refreshed_at = time.time()
            elif e.code == 403 and "quotaExceeded" in body:
                print("Quota exceeded, backing off 15 min...", file=sys.stderr, flush=True)
                time.sleep(900)
                continue
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
                cid = author.get("channelId", "")
                if cid:
                    seen_users[cid] = time.time()
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

            WELCOME_BACK_THRESHOLD = 12 * 3600  # 12 hours

            if channel_id:
                last_seen = seen_users.get(channel_id)
                now = time.time()
                if last_seen is None:
                    # First time seeing this user
                    events.append({
                        "type": "join",
                        "name": name,
                        "time": now,
                    })
                elif now - last_seen >= WELCOME_BACK_THRESHOLD:
                    # Returning after 12+ hours
                    events.append({
                        "type": "welcome_back",
                        "name": name,
                        "time": now,
                    })
                seen_users[channel_id] = now

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
