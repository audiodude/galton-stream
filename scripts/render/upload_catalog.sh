#!/usr/bin/env bash
# upload_catalog.sh <out_dir> <s3_uri>
# Syncs rendered mp4s first, then uploads catalog.json last to ensure
# the manifest only goes live once all pieces are in place.
set -euo pipefail

OUT_DIR="${1:?usage: upload_catalog.sh <out_dir> <s3_uri>}"
S3_URI="${2:?}"

[[ -d "$OUT_DIR" ]] || { echo "out_dir not found: $OUT_DIR" >&2; exit 1; }
[[ -f "$OUT_DIR/catalog.json" ]] || { echo "catalog.json not found in $OUT_DIR — run make_manifest.py first" >&2; exit 1; }

echo "[upload_catalog] syncing mp4s: $OUT_DIR -> $S3_URI"
aws s3 sync "$OUT_DIR" "$S3_URI" \
    --exclude "*" \
    --include "*.mp4" \
    --no-progress

echo "[upload_catalog] uploading manifest: $S3_URI/catalog.json"
aws s3 cp "$OUT_DIR/catalog.json" "$S3_URI/catalog.json"

echo "[upload_catalog] done"
