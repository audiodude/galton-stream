# Galton Board Simulator

A Godot 4.x app that simulates a Galton board (bean machine). Balls drop through pegs and collect in bins, forming a bell curve. Designed to run 24/7 and be streamed to YouTube.

## Features

- Balls spawn continuously from a funnel at the top
- 12 rows of pegs with concave curved walls
- 2-3 randomly-selected colors per round
- Bins show blended colors from all balls that landed in them
- Histogram overlay scales to full bin height
- Pegs flash subtly when touched
- Auto-resets after 472-512 balls per round
- Ball/cycle counter
- Live chat integration (welcomes, gift alerts)

## Requirements

- Godot 4.4+
- Python 3 (for chat services)

## Running

```bash
godot --path . --main-scene main.tscn
```

Press ESC to quit.

## Chat Event IPC

Godot and the chat service communicate via a shared JSON file (`/tmp/chat_events.json` by default, configurable via `CHAT_EVENTS_FILE` env var).

### Protocol

The chat service writes a JSON array of event objects to the file. Godot reads and deletes the file each second. Write atomically (write to `.tmp`, then rename) to avoid partial reads.

### Event format

Each event is a JSON object with a `type` field:

```json
[
  {"type": "join", "name": "UserName", "time": 1712345678.9},
  {"type": "message", "name": "UserName", "text": "hello!", "time": 1712345678.9},
  {"type": "gift", "name": "UserName", "amount": "$10", "time": 1712345678.9}
]
```

| Field    | Type   | Description                          |
|----------|--------|--------------------------------------|
| `type`   | string | `"join"`, `"message"`, or `"gift"`   |
| `name`   | string | Display name of the user             |
| `text`   | string | Chat message (message events only)   |
| `amount` | string | Gift amount (gift events only)       |
| `time`   | float  | Unix timestamp                       |

### Chat services

**Mock (testing):**
```bash
python3 scripts/mock_youtube.py                    # all event types
python3 scripts/mock_youtube.py --joins-only       # only joins
python3 scripts/mock_youtube.py --gifts-only       # only gifts
python3 scripts/mock_youtube.py --interval 3       # every 3 seconds
python3 scripts/mock_youtube.py --burst 5          # up to 5 events per batch
```

**Live YouTube:**
```bash
YOUTUBE_API_KEY=... YOUTUBE_VIDEO_ID=... python3 scripts/chat_poller.py
```

## Streaming (Docker)

Deploy via Railway or run locally:

```bash
docker build -t galton-stream .
docker run -e YOUTUBE_STREAM_KEY=your-key galton-stream
```

The container runs Godot headless with Xvfb and streams to YouTube via FFmpeg.
