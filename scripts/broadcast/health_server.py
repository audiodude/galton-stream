#!/usr/bin/env python3
"""Minimal /health endpoint for monitor.py. Returns JSON {status} matching galton-stream contract."""
import http.server, json, os, time

HEARTBEAT = os.environ.get("PLAYOUT_HEARTBEAT", "/tmp/playout_heartbeat")
MAX_AGE = float(os.environ.get("PLAYOUT_HEARTBEAT_MAX_AGE", "30"))

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        fresh = os.path.exists(HEARTBEAT) and (time.time() - os.path.getmtime(HEARTBEAT)) < MAX_AGE
        body = json.dumps({"status": "alive" if fresh else "dead"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

if __name__ == "__main__":
    http.server.HTTPServer(("0.0.0.0", int(os.environ.get("PLAYOUT_HEALTH_PORT", "8080"))), H).serve_forever()
