#!/usr/bin/env python3
"""Minimal /health endpoint for monitor.py. 200 while a heartbeat file is fresh."""
import http.server, os, time

HEARTBEAT = os.environ.get("PLAYOUT_HEARTBEAT", "/tmp/playout_heartbeat")
MAX_AGE = float(os.environ.get("PLAYOUT_HEARTBEAT_MAX_AGE", "30"))

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        fresh = os.path.exists(HEARTBEAT) and (time.time() - os.path.getmtime(HEARTBEAT)) < MAX_AGE
        self.send_response(200 if fresh else 503)
        self.end_headers()
        self.wfile.write(b"ok" if fresh else b"stale")
    def log_message(self, *a):
        pass

if __name__ == "__main__":
    http.server.HTTPServer(("0.0.0.0", int(os.environ.get("PLAYOUT_HEALTH_PORT", "8080"))), H).serve_forever()
