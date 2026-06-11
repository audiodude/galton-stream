#!/usr/bin/env bash
# sync_down.sh <workdir>
# Downloads the station's mp3 library from S3 into <workdir>/original/.
# Idempotent — only downloads new or changed files (aws s3 sync).
#
# Requires: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION, S3_MUSIC_BUCKET
# Source env.sh first if needed: source scripts/library/env.sh
set -euo pipefail

WORKDIR="${1:?usage: sync_down.sh <workdir>}"
: "${S3_MUSIC_BUCKET:?S3_MUSIC_BUCKET is not set — source env.sh first}"

DEST="$WORKDIR/original"
mkdir -p "$DEST"

echo "[sync_down] $S3_MUSIC_BUCKET -> $DEST (mp3 only)"
aws s3 sync "$S3_MUSIC_BUCKET" "$DEST" \
    --exclude "*" \
    --include "*.mp3"
echo "[sync_down] done"
