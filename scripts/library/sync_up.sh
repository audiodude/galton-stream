#!/usr/bin/env bash
# sync_up.sh <workdir>
# Uploads ONLY trimmed files from <workdir>/trimmed/ to S3, overwriting their originals.
# Non-offenders are not touched in S3.
#
# !!! PRODUCTION IMPACT !!!
# After upload, the Railway volume cache (/data/mp3) must be MANUALLY CLEARED
# before the station picks up the new files. start.sh skips S3 sync when /data/mp3
# is non-empty — clearing that dir (or the volume) triggers a fresh sync on next deploy.
#
# Do NOT run this script until trimmed files have been reviewed.
#
# Requires: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION, S3_MUSIC_BUCKET
# Source env.sh first if needed: source scripts/library/env.sh
set -euo pipefail

WORKDIR="${1:?usage: sync_up.sh <workdir>}"
: "${S3_MUSIC_BUCKET:?S3_MUSIC_BUCKET is not set — source env.sh first}"

TRIM_DIR="$WORKDIR/trimmed"

if [[ ! -d "$TRIM_DIR" ]]; then
    echo "error: $TRIM_DIR does not exist — run trim.py first" >&2
    exit 1
fi

# Collect trimmed mp3s
mapfile -t FILES < <(find "$TRIM_DIR" -maxdepth 1 -name "*.mp3" | sort)

if [[ ${#FILES[@]} -eq 0 ]]; then
    echo "[sync_up] no trimmed files found in $TRIM_DIR — nothing to upload"
    exit 0
fi

echo "[sync_up] uploading ${#FILES[@]} trimmed files to $S3_MUSIC_BUCKET"
echo ""

for src in "${FILES[@]}"; do
    name="$(basename "$src")"
    dest="${S3_MUSIC_BUCKET%/}/$name"
    echo "  uploading: $name -> $dest"
    aws s3 cp "$src" "$dest"
done

echo ""
echo "[sync_up] done — ${#FILES[@]} files uploaded"
echo ""
echo "!!! REMINDER: clear /data/mp3 on the Railway volume to force a fresh S3 sync on next deploy !!!"
