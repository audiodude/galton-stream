# vxstory monitor

Observes the Hetzner playout box, owns the YouTube broadcast lifecycle (scoped
to its own stream), drives the radio.dangerthirdrail.com redirect, alerts.

## Required env (Railway service `galton-monitor` / `vxstory-monitor`)
- `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`
- `YOUTUBE_STREAM_KEY` — the owned production stream key (the box pushes to this).
- `BROADCAST_TITLE`, `BROADCAST_DESCRIPTION`, `BROADCAST_PRIVACY` (default `public`)
- `BOX_HEALTH_URL`, `BOX_HEALTH_TOKEN` — box `/health` + shared secret.
- `RADIO_BUCKET`, `RADIO_CF_DISTRIBUTION_ID`, `RADIO_REGION`, `RADIO_OFFLINE_HTML_PATH`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- Optional: `POLL_INTERVAL` (120), `DEGRADED_GRACE_POLLS` (3), `FORCE_ACTIVE` (`1` = always-on)

## Removed vs galton-monitor-orig
`GALTON_STREAM_URL`, `RAILWAY_API_TOKEN`, `GALTON_STREAM_SERVICE_ID`, and all
chat/title/fallback/Railway-restart logic. Window definition must match the box's
playout scheduling.
