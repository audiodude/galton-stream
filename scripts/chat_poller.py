#!/usr/bin/env python3
"""Streams YouTube live chat via the gRPC streamList endpoint.

Polling liveChatMessages.list through the REST API burns the default
10,000-unit daily quota in about three hours at 5-second intervals.
streamList is Google's low-latency push alternative: open one gRPC
server-streaming connection and messages are pushed as they arrive.
Quota impact in practice is negligible vs. tight polling.

Broadcast discovery still uses REST (liveBroadcasts.list, 1 unit) when
YOUTUBE_LIVE_CHAT_ID isn't set. Everything else rides the gRPC stream.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import grpc

# Generated protobuf modules live next to this script.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stream_list_pb2
import stream_list_pb2_grpc

from chat_events import write_events

TOKEN_FILE = os.environ.get("YOUTUBE_TOKEN_FILE", "youtube_token.json")
LIVE_CHAT_ID = os.environ.get("YOUTUBE_LIVE_CHAT_ID", "")
GRPC_TARGET = "dns:///youtube.googleapis.com:443"

# Access tokens are good for ~1 hour; refresh well before expiry so an
# in-flight stream can be rotated during the next reconnect.
TOKEN_REFRESH_INTERVAL = 2700  # 45 minutes

WELCOME_BACK_THRESHOLD = 12 * 3600  # 12 hours

# LiveChatMessageSnippet.TypeWrapper.Type enum values (from stream_list.proto)
TYPE_TEXT_MESSAGE = 1
TYPE_SUPER_CHAT = 15
TYPE_SUPER_STICKER = 16


# ---------- OAuth (same as before) ----------

def load_token_config():
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
    if not os.path.exists(TOKEN_FILE):
        print(
            f"ERROR: No YouTube OAuth credentials. Set YOUTUBE_CLIENT_ID, "
            f"YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN env vars, "
            f"or create {TOKEN_FILE}.",
            file=sys.stderr,
        )
        sys.exit(1)
    with open(TOKEN_FILE) as f:
        return json.load(f)


def get_access_token(config):
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


def _format_http_error(e):
    try:
        body = e.read().decode()
    except Exception:
        body = ""
    return f"HTTP {e.code}: {body}"


def get_access_token_retrying(config):
    backoff = 30
    while True:
        try:
            return get_access_token(config)
        except urllib.error.HTTPError as e:
            print(f"Token refresh failed: {_format_http_error(e)}",
                  file=sys.stderr, flush=True)
        except (urllib.error.URLError, OSError) as e:
            print(f"Token refresh network error: {e}",
                  file=sys.stderr, flush=True)
        print(f"Retrying token refresh in {backoff}s...",
              file=sys.stderr, flush=True)
        time.sleep(backoff)
        backoff = min(backoff * 2, 900)


# ---------- Broadcast discovery (REST, 1 unit per call) ----------

def api_get(url, access_token):
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {access_token}")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def find_active_broadcast(access_token):
    url = (
        "https://www.googleapis.com/youtube/v3/liveBroadcasts"
        "?part=snippet,contentDetails&broadcastStatus=active&broadcastType=all"
    )
    try:
        data = api_get(url, access_token)
    except urllib.error.HTTPError as e:
        print(f"Broadcast API error: {_format_http_error(e)}",
              file=sys.stderr, flush=True)
        return None, None
    items = data.get("items", [])
    if not items:
        return None, None
    broadcast = items[0]
    return broadcast["snippet"]["title"], broadcast["snippet"]["liveChatId"]


# ---------- gRPC streaming ----------

def _item_to_events(item, seen_users, first_pass):
    """Map a LiveChatMessage proto to 0+ IPC events for Godot."""
    snippet = item.snippet
    author = item.author_details
    msg_type = snippet.type
    name = author.display_name or "Unknown"
    channel_id = author.channel_id
    now = time.time()

    events = []

    # Welcome / welcome-back tracking fires for any message from a user —
    # join events in the old REST poller were synthesized the same way.
    if channel_id and not first_pass:
        last = seen_users.get(channel_id)
        if last is None:
            events.append({"type": "join", "name": name, "time": now})
        elif now - last >= WELCOME_BACK_THRESHOLD:
            events.append({"type": "welcome_back", "name": name, "time": now})
    if channel_id:
        seen_users[channel_id] = now

    if msg_type == TYPE_SUPER_CHAT:
        events.append({
            "type": "gift",
            "name": name,
            "amount": snippet.super_chat_details.amount_display_string,
            "time": now,
        })
    elif msg_type == TYPE_SUPER_STICKER:
        events.append({
            "type": "sticker",
            "name": name,
            "amount": snippet.super_sticker_details.amount_display_string,
            "time": now,
        })

    return events


def run():
    config = load_token_config()
    access_token = get_access_token_retrying(config)
    token_refreshed_at = time.time()

    if LIVE_CHAT_ID:
        chat_id = LIVE_CHAT_ID
        print(f"Using chat ID from env: {chat_id}", flush=True)
    else:
        print("Looking for active broadcast via REST...", flush=True)
        chat_id = None
        backoff = 30
        while not chat_id:
            title, chat_id = find_active_broadcast(access_token)
            if not chat_id:
                print(f"No active broadcast found. Retrying in {backoff}s...",
                      flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 900)
                if time.time() - token_refreshed_at > TOKEN_REFRESH_INTERVAL:
                    access_token = get_access_token_retrying(config)
                    token_refreshed_at = time.time()
        print(f"Found broadcast: {title}", flush=True)
        print(f"Chat ID: {chat_id}", flush=True)

    creds = grpc.ssl_channel_credentials()
    # Keepalive so the connection survives upstream NAT timeouts.
    options = [
        ("grpc.keepalive_time_ms", 60_000),
        ("grpc.keepalive_timeout_ms", 20_000),
        ("grpc.keepalive_permit_without_calls", 1),
        ("grpc.http2.max_pings_without_data", 0),
    ]
    channel = grpc.secure_channel(GRPC_TARGET, creds, options=options)
    stub = stream_list_pb2_grpc.V3DataLiveChatMessageServiceStub(channel)

    seen_users = {}
    first_pass = True
    next_page_token = ""
    reconnect_delay = 5

    while True:
        if time.time() - token_refreshed_at > TOKEN_REFRESH_INTERVAL:
            try:
                access_token = get_access_token_retrying(config)
                token_refreshed_at = time.time()
                print("[chat] Access token refreshed", flush=True)
            except Exception as e:
                print(f"[chat] Token refresh error: {e}",
                      file=sys.stderr, flush=True)

        metadata = (("authorization", f"Bearer {access_token}"),)

        try:
            request = stream_list_pb2.LiveChatMessageListRequest(
                part=["snippet", "authorDetails"],
                live_chat_id=chat_id,
                max_results=200,
                page_token=next_page_token,
            )
            print(
                f"[chat] Opening stream "
                f"(resume={'yes' if next_page_token else 'no'}, "
                f"first_pass={first_pass})",
                flush=True,
            )

            got_any = False
            for response in stub.StreamList(request, metadata=metadata):
                got_any = True
                events = []
                for item in response.items:
                    events.extend(_item_to_events(item, seen_users, first_pass))

                if response.next_page_token:
                    next_page_token = response.next_page_token

                if first_pass:
                    continue  # drop initial history snapshot

                if events:
                    write_events(events)
                    for e in events:
                        print(f"  {e['type']}: {e['name']}", flush=True)

            if first_pass:
                first_pass = False
                print(
                    f"[chat] Initial catch-up complete, "
                    f"{len(seen_users)} known users",
                    flush=True,
                )

            if not got_any:
                print("[chat] Stream ended with no responses; reconnecting",
                      flush=True)

            reconnect_delay = 5
        except grpc.RpcError as e:
            code = e.code() if hasattr(e, "code") else None
            details = e.details() if hasattr(e, "details") else str(e)
            print(f"[chat] gRPC error {code}: {details}",
                  file=sys.stderr, flush=True)
            if code == grpc.StatusCode.UNAUTHENTICATED:
                # Force token refresh on next iteration.
                token_refreshed_at = 0
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 300)


if __name__ == "__main__":
    run()
