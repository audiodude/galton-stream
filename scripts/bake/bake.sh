#!/usr/bin/env bash
# Nightly bake: sync inputs from S3, plan -> assemble -> validate -> publish.
# Env: S3_MUSIC_BUCKET, S3_CATALOG_URI, SHOWS_DIR, S3_SHOWS_URI, AWS creds,
#      BAKE_RES (default 1280x720), BAKE_FPS (60), BAKE_DWELL (600),
#      SHOW_DUR (default 22800), BAKE_VBITRATE (6M), RETENTION_DAYS (3).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DATE="$(date +%F)"
SHOWS_DIR="${SHOWS_DIR:-/data/shows}"
MUSIC_DIR="${MUSIC_DIR:-/data/mp3}"
CATALOG_DIR="${CATALOG_DIR:-/data/catalog}"
mkdir -p "$SHOWS_DIR" "$MUSIC_DIR" "$CATALOG_DIR"

echo "[bake] syncing inputs from S3"
aws s3 sync "${S3_MUSIC_BUCKET:?}" "$MUSIC_DIR/" --no-progress --exclude "*" --include "*.mp3"
aws s3 sync "${S3_CATALOG_URI:?}" "$CATALOG_DIR/" --no-progress

PLAN="$SHOWS_DIR/show-$DATE.plan.json"
SHOW="$SHOWS_DIR/show-$DATE.mkv"
SEED="$(date +%Y%m%d)"

echo "[bake] planning (seed=$SEED res=${BAKE_RES:-1280x720})"
python3 "$HERE/plan.py" --catalog "$CATALOG_DIR/catalog.json" --library-dir "$MUSIC_DIR" \
    --seed "$SEED" --show-dur "${SHOW_DUR:-22800}" --dwell "${BAKE_DWELL:-600}" \
    --fps "${BAKE_FPS:-60}" --resolution "${BAKE_RES:-1280x720}" --out "$PLAN"

echo "[bake] assembling"
BAKE_VBITRATE="${BAKE_VBITRATE:-6M}" python3 "$HERE/assemble.py" --plan "$PLAN" --out "$SHOW"

echo "[bake] validating"
python3 "$HERE/validate.py" --plan "$PLAN" --show "$SHOW"

echo "[bake] publishing to S3"
aws s3 cp "$SHOW" "${S3_SHOWS_URI:?}/show-$DATE.mkv" --no-progress
aws s3 cp "$PLAN" "${S3_SHOWS_URI}/show-$DATE.plan.json" --no-progress
echo "show-$DATE.mkv" > "$SHOWS_DIR/LATEST"
aws s3 cp "$SHOWS_DIR/LATEST" "${S3_SHOWS_URI}/LATEST" --no-progress

echo "[bake] pruning shows older than ${RETENTION_DAYS:-3} days"
find "$SHOWS_DIR" -name 'show-*.mkv' -mtime "+${RETENTION_DAYS:-3}" -delete || true
echo "[bake] done: $SHOW"
