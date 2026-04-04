#!/usr/bin/env python3
"""Mock YouTube chat service for local testing.

Generates fake join, message, and gift events at configurable rates.
Writes to the same JSON IPC file that Godot reads.

Usage:
    python3 mock_youtube.py
    python3 mock_youtube.py --interval 3 --burst 5
    python3 mock_youtube.py --joins-only
"""

import argparse
import random
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

# Weighted event types: joins are most common, gifts/stickers are rare
EVENT_WEIGHTS = {
    "join": 5,
    "message": 3,
    "gift": 1,
    "sticker": 1,
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


def pick_event_type(allowed_types):
    types = []
    weights = []
    for t in allowed_types:
        types.append(t)
        weights.append(EVENT_WEIGHTS[t])
    return random.choices(types, weights=weights, k=1)[0]


def run(interval, max_burst, allowed_types):
    print(f"Mock YouTube service started (interval={interval}s, max_burst={max_burst})")
    print(f"Event types: {', '.join(allowed_types)}")

    while True:
        # Stagger individual events with random delays
        num_events = random.randint(0, max_burst)
        for i in range(num_events):
            event = make_event(pick_event_type(allowed_types))
            write_events([event])
            label = event["type"].upper().ljust(8)
            print(f"  {label} {event['name']}", flush=True)

            if i < num_events - 1:
                time.sleep(random.uniform(1.0, 4.0))

        if not num_events:
            print("  (no events)", flush=True)

        time.sleep(random.uniform(interval * 0.5, interval * 1.5))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mock YouTube chat events")
    parser.add_argument("--interval", type=float, default=5, help="Seconds between batches")
    parser.add_argument("--burst", type=int, default=3, help="Max events per batch")
    parser.add_argument("--joins-only", action="store_true", help="Only generate join events")
    parser.add_argument("--gifts-only", action="store_true", help="Only generate gift events")
    parser.add_argument("--stickers-only", action="store_true", help="Only generate sticker events")
    args = parser.parse_args()

    if args.joins_only:
        types = ["join"]
    elif args.gifts_only:
        types = ["gift"]
    elif args.stickers_only:
        types = ["sticker"]
    else:
        types = list(EVENT_WEIGHTS.keys())

    run(args.interval, args.burst, types)
