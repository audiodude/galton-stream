"""Telegram alerting with edge-triggered incident discipline: page when we drop
into DEGRADED, and again when the incident clears. Routine churn stays quiet."""
import os
import sys
import urllib.parse
import urllib.request

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PREFIX = "vxstory monitor:"


def _log(msg):
    print(f"[monitor] {msg}", file=sys.stderr, flush=True)


class Alerter:
    def __init__(self):
        self.state = "OFF"
        self.incident = False

    def send(self, text):
        if not BOT_TOKEN or not CHAT_ID:
            _log(f"(no telegram) {text}")
            return
        try:
            data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=data)
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            _log(f"Telegram send failed: {e}")

    def update(self, state, reason):
        if state == self.state:
            return
        msg = f"{PREFIX} {self.state} -> {state}. {reason}"
        _log(msg)
        if state == "DEGRADED":
            if not self.incident:
                self.incident = True
                self.send(msg)
        elif self.incident:           # any move out of DEGRADED clears the incident
            self.incident = False
            self.send(msg)
        self.state = state
