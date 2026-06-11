#!/usr/bin/env bash
# env.sh — source this to load AWS + S3 credentials from Railway into the current shell.
# Usage: source scripts/library/env.sh
#
# Requires: railway CLI linked to this project (railway link).
# Does NOT print secret values.

eval "$(railway variables --json | python3 -c "
import json, sys, shlex
d = json.load(sys.stdin)
for k in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_DEFAULT_REGION', 'S3_MUSIC_BUCKET'):
    if k in d:
        print(f'export {k}={shlex.quote(d[k])}')
")"
echo "[env] AWS credentials and S3_MUSIC_BUCKET loaded." >&2
