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
- Background music from S3 with on-screen song title display

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

## Music

The stream plays background music from MP3 files stored in S3. A Python music player decodes tracks and pipes raw audio to FFmpeg via a named pipe.

### How it works

1. On startup, `start.sh` syncs MP3s from S3 to `/data/mp3` (a persistent volume)
2. `scripts/music_player.py` shuffles and loops through all tracks
3. Each track is decoded to raw PCM (s16le, 44.1kHz stereo) and written to `/tmp/audio_pipe`
4. FFmpeg reads from the pipe and muxes the audio with the video capture
5. The current song title is written to `/tmp/current_song.txt`
6. `scripts/song_display.gd` polls that file every 2s and shows "♪ Song Title" in the bottom-left corner

### Song title display

When a new song starts, the title appears at full opacity for 5 seconds, then fades to 0.4 over 10 seconds and holds there until the next track.

### Environment variables

| Variable              | Description                           |
|-----------------------|---------------------------------------|
| `S3_MUSIC_BUCKET`       | S3 URI for music files (e.g. `s3://bucket/path/`) |
| `AWS_ACCESS_KEY_ID`     | AWS credentials for S3 music sync     |
| `AWS_SECRET_ACCESS_KEY` | AWS credentials for S3 music sync     |

The S3 bucket path is configured in `start.sh`. Music files are cached on a persistent volume at `/data/mp3` so they only sync once.

## Deployment

Railway auto-deploys both services from the `release` branch (not `main`). Push to `main` for development, then merge to `release` to deploy.

### Two-service architecture

| Service | Purpose | Watch paths |
|---------|---------|-------------|
| **galton-stream** | Godot + FFmpeg streaming to YouTube | Everything except `monitor/` |
| **galton-monitor** | Manages the day's YouTube broadcast, polls galton-stream health, updates the radio redirect | `monitor/**` |

### Daily broadcast lifecycle

YouTube deranks channels that stream 24/7. galton-monitor creates a fresh broadcast at window open (12:00 PT) and tears it down at window close (18:00 PT):

- **At window open** — clone metadata (title, description, etc.) from the most recent broadcast via the YouTube API, create a new broadcast with `ultraLow` latency, and bind the stable `liveStream` (so `YOUTUBE_STREAM_KEY` is unchanged). Once the broadcast goes live, update `radio.dangerthirdrail.com` to redirect to `youtube.com/live/<new_video_id>`.
- **At window close** — transition the broadcast to `complete`, set privacy to `private`, and flip the radio redirect to the offline title card.

The radio redirect is implemented as an S3 website bucket (`radio.dangerthirdrail.com`) fronted by CloudFront. Online = bucket routing rule 301s to YouTube. Offline = routing rule is removed and `index.html` (a responsive title card) is served.

### Recovery escalation (within the active window)

galton-monitor polls galton-stream's `/health` endpoint every 120s over Railway internal networking:

1. **1 fail (120s)** — start fallback stream (backup image to YouTube)
2. **5 fails (600s)** — restart galton-stream container
3. **6 fails (720s)** — redeploy galton-stream via Railway API
4. **7 fails (840s)** — alert that all recovery failed

### Running locally

```bash
docker build -t galton-stream .
docker run -e YOUTUBE_STREAM_KEY=your-key \
           -e S3_MUSIC_BUCKET=s3://bucket/path/ \
           -e AWS_ACCESS_KEY_ID=... \
           -e AWS_SECRET_ACCESS_KEY=... \
           galton-stream
```

The container runs Godot headless with Xvfb and streams to YouTube via FFmpeg at 720p 30fps with audio.
