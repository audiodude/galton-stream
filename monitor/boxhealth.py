"""Probe the Hetzner box /health endpoint (diagnostic enrichment for alerts;
never gates a lifecycle decision). Bearer-token auth since it's public-facing."""
import json
import os
import sys
import urllib.error
import urllib.request

BOX_HEALTH_URL = os.environ.get("BOX_HEALTH_URL", "")
BOX_HEALTH_TOKEN = os.environ.get("BOX_HEALTH_TOKEN", "")


def _log(msg):
    print(f"[monitor] {msg}", file=sys.stderr, flush=True)


def probe():
    if not BOX_HEALTH_URL:
        return None
    headers = {}
    if BOX_HEALTH_TOKEN:
        headers["Authorization"] = f"Bearer {BOX_HEALTH_TOKEN}"
    try:
        req = urllib.request.Request(BOX_HEALTH_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        _log(f"box health probe failed: {e}")
        return None
