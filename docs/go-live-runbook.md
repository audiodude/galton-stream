# Go-live runbook (vxstory pre-baked playout)

The steady state: **render → S3 catalog → nightly bake (03:00 PT) → window-gated
playout (11:45–18:05 PT) → monitor owns the YouTube broadcast + redirect.**

## Add content (render host, needs the GPU)

    cd ~/code/vibes/radio.dangerthirdrail.com
    # spec = [{model, preset, seed, duration_sec, kind}] — see scripts/render/specs/
    ./scripts/render/render_catalog.sh scripts/render/specs/<spec>.json catalog_v2
    eval "$(ssh -n radio-playout 'sudo grep -E "^AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY|DEFAULT_REGION)=" /etc/radio.env' | sed 's/^/export /')"
    ./scripts/render/upload_catalog.sh catalog_v2 s3://music-backup-996646211514-us-west-1-an/catalog

`render_catalog.sh` skips pieces whose mp4 already exists, so it is resumable.
`upload_catalog.sh` uploads mp4s first and `catalog.json` last — a bake that
starts mid-upload still sees a consistent manifest. The next nightly bake picks
new pieces up automatically; nothing else has to be touched.

Roughly 30 min of render time per 300 s piece on an RTX 3080 Ti (1080p60 into
a temp AVI, Lanczos-downscaled to 720p at CRF 18).

### Piece length sets the music budget

`plan.py` cuts video at song boundaries: a piece segment runs `dwell + the rest
of the current song`, so it must fit inside the **shortest** piece. Songs longer
than `min_piece - dwell` are dropped from the show (listed as `excluded_songs`
in the plan, and printed by the bake). With 300 s pieces and `BAKE_DWELL=30`
that drops 3 of 165 tracks. Want the long tracks on air? Render longer pieces.

## Bake by hand (normally the timer does this)

    ssh radio-playout 'sudo systemctl start radio-bake.service'   # ~2.5 h for a 6 h show
    ssh radio-playout 'journalctl -u radio-bake -f'

Output: `/data/shows/show-<date>.mkv` + `.plan.json`, published to S3, with
`/data/shows/LATEST` naming the current show. `RETENTION_DAYS=3`.

## Go live

    ssh radio-playout 'sudo systemctl enable --now radio-bake.timer'
    ssh radio-playout 'sudo systemctl enable --now radio-playout.service'
    # resume the Railway monitor (owns broadcast lifecycle + redirect)
    curl -sS -X POST https://backboard.railway.com/graphql/v2 \
      -H "Authorization: Bearer $CLAUDE_RAILWAY_TOKEN" -H "Content-Type: application/json" \
      -d '{"query":"mutation { deploymentRedeploy(id: \"<latest-deployment-id>\") { id status } }"}'

Both sides self-gate to the operational window, so enabling them outside it is
safe — playout idles until 11:45 PT and the monitor sleeps to the next boundary.

## Verify (during the window)

- `ssh radio-playout 'systemctl status radio-playout'` — ffmpeg running, streaming `/data/shows/show-*.mkv`.
- `curl -H "Authorization: Bearer $BOX_HEALTH_TOKEN" https://radio-sys.dangerthirdrail.com/health` — `playout_alive`, `ffmpeg_alive`, fresh `heartbeat_age_s`.
- YouTube Studio: stream `active` → broadcast `live`.
- `radio.dangerthirdrail.com` redirects to the live video from 12:00 PT.
- At 18:05 PT: box stops → broadcast completes → redirect goes offline.

## Kill switch

    ssh radio-playout 'sudo systemctl stop radio-playout'          # off air now
    ssh radio-playout 'sudo systemctl disable radio-playout radio-bake.timer'

Stopping playout ends the stream; the monitor completes the broadcast on its
next poll (≤2 min). Never restart playout twice concurrently — `playout.sh`
holds `/tmp/playout.lock` for exactly that reason.
