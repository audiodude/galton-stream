---
name: Stream reliability debugging session 2026-04-04
description: Fixed stream freezing at song transitions (pipe EOF), song title desync, and added Telegram watchdog monitoring
type: project
---

## Session summary

Debugged and fixed several issues with the Galton Board YouTube stream.

### Stream hanging after ~1 hour
- Initial hypothesis: FFmpeg input queue overflow over time. Added `-thread_queue_size 4096` on both inputs and increased `-bufsize` to 7500k (3x bitrate).
- **Root cause turned out to be pipe EOF at song transitions** (see below).

### Stream freezing at every song transition
- The music_player.py was spawning a separate `ffmpeg` decoder per song, writing directly to the named pipe `/tmp/audio_pipe`. When each decoder exited, it closed its end of the FIFO, causing the streaming FFmpeg to see EOF and stall.
- **Fix:** Python opens the pipe fd once with `os.open()` and keeps it open permanently. Each decoder writes to `pipe:1` (stdout), and Python copies chunks to the pipe fd. The FIFO never sees EOF between songs.
- First attempted fix (sleep between songs) made it worse — blocked the pipe entirely.

### Song title displaying next track too early
- The large `thread_queue_size` let the streaming FFmpeg buffer audio far ahead of real-time. The decoder finished minutes before the song was audible, and the title changed immediately.
- First fix attempt: `time.sleep(remaining)` after decode — froze the stream (blocked the pipe).
- Second fix attempt: `threading.Timer` scheduled per song — each new timer cancelled the previous one, so only the last song's timer survived. Title got stuck.
- **Final fix:** A `queue.Queue` of `(absolute_time, title)` pairs processed by a dedicated background thread. The thread sleeps until each title's scheduled time, processing them in order without cancellation.

### Watchdog / Telegram alerting
- Added `scripts/watchdog.sh` that monitors the streaming FFmpeg process and network TX bytes. Alerts via Telegram (prefixed "Galton monitor:") if the process dies or bytes stall for 3 consecutive 60s checks.
- Initial version used `pgrep -x ffmpeg` which matched decoder instances too. Fixed to use `pgrep -f "flv.*rtmp"` to find only the streaming FFmpeg.
- Uses `/proc/net/dev` for container-level TX byte counting.
- Requires `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` env vars in Railway (set during this session).

### Song title color flash effect
- Added color cycling through current round colors when a new song starts (1.5s at 100ms intervals), then settles to the board label color `Color(0.6, 0.65, 0.8, 0.7)` and fades to 0.4 opacity over 10s.

### Deployment
- Railway auto-deploys from the `release` branch (documented in README this session).
- Workflow: commit to main, push, merge to release, push release.

### Key architectural insight
**Why:** Named pipes (FIFOs) deliver EOF to the reader when the last writer closes. In a producer-consumer audio pipeline, the write end must stay open across song boundaries or the consumer stalls.
**How to apply:** Any future changes to the music pipeline must preserve the single persistent pipe fd pattern. Never let individual decoder processes open/close the FIFO directly.
