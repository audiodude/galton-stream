#!/usr/bin/env python3
"""Mock YouTube chat service for local testing.

Generates fake chat events at configurable rates per type.
Writes to the same JSON IPC file that Godot reads.

Usage:
    python3 mock_youtube.py
    python3 mock_youtube.py --join-delay-ms 3000 --gift-delay-ms 10000
    python3 mock_youtube.py --join-delay-ms 2000  # only joins every 2s
"""

import argparse
import random
import threading
import time

from chat_events import write_events

NAMES = [
    "Deputadodigital", "DonSalieri181", "Espen005", "Hiuyt543",
    "LofiLibraryVibe", "Marlonhof", "NerdOdyssey", "YenXenshii_rHh",
    "affiliation5475", "bandit12116", "cyberbullka", "davidboyd6947",
    "jonathanbecerra9579", "joseoscar8429", "marenkujio3786",
    "memelife0078", "mingfanzhang8927", "sarahmast",
    "stephaniesilvester5728", "thestoicbean1218", "vagnurt",
    "NamoAmitabha南無阿彌陀佛",
]

MESSAGES = [
    "love this!", "so satisfying", "which bin will win?",
    "the colors are amazing", "hypnotic", "can't stop watching",
    "GO LEFT!", "GO RIGHT!", "bell curve baby!",
    "this is oddly relaxing", "wow", "amazing",
]

GIFT_AMOUNTS = ["$2", "$5", "$10", "$20", "$50", "$100"]
STICKER_AMOUNTS = ["$1", "$2", "$5", "$10"]

# Default delays in ms (0 = disabled)
DEFAULT_DELAYS = {
    "join": 5000,
    "welcome_back": 8000,
    "message": 4000,
    "gift": 15000,
    "sticker": 15000,
}


def make_event(event_type):
    name = random.choice(NAMES)
    event = {"type": event_type, "name": name, "time": time.time()}

    if event_type == "message":
        event["text"] = random.choice(MESSAGES)
    elif event_type == "gift":
        event["amount"] = random.choice(GIFT_AMOUNTS)
    elif event_type == "sticker":
        event["amount"] = random.choice(STICKER_AMOUNTS)

    return event


def run_type(event_type, delay_ms):
    delay_s = delay_ms / 1000.0
    while True:
        # Add some jitter (±30%)
        jitter = random.uniform(0.7, 1.3)
        time.sleep(delay_s * jitter)
        event = make_event(event_type)
        write_events([event])
        label = event["type"].upper().ljust(15)
        print(f"  {label} {event['name']}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mock YouTube chat events")
    parser.add_argument("--join-delay-ms", type=int, default=None,
                        help="Delay between join events in ms (default: 5000)")
    parser.add_argument("--welcome-back-delay-ms", type=int, default=None,
                        help="Delay between welcome_back events in ms (default: 8000)")
    parser.add_argument("--message-delay-ms", type=int, default=None,
                        help="Delay between message events in ms (default: 4000)")
    parser.add_argument("--gift-delay-ms", type=int, default=None,
                        help="Delay between gift events in ms (default: 15000)")
    parser.add_argument("--sticker-delay-ms", type=int, default=None,
                        help="Delay between sticker events in ms (default: 15000)")
    args = parser.parse_args()

    delays = {
        "join": args.join_delay_ms,
        "welcome_back": args.welcome_back_delay_ms,
        "message": args.message_delay_ms,
        "gift": args.gift_delay_ms,
        "sticker": args.sticker_delay_ms,
    }

    # If any flag is explicitly set, only run those types.
    # Otherwise use all defaults.
    explicit = {k: v for k, v in delays.items() if v is not None}
    if explicit:
        active = explicit
    else:
        active = DEFAULT_DELAYS

    print("Mock YouTube service started")
    for etype, ms in active.items():
        print(f"  {etype}: every {ms}ms", flush=True)

    threads = []
    for etype, ms in active.items():
        t = threading.Thread(target=run_type, args=(etype, ms), daemon=True)
        t.start()
        threads.append(t)

    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
