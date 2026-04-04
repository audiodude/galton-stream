#!/usr/bin/env python3
"""One-time OAuth flow for YouTube API. Saves refresh token to a file."""

import http.server
import json
import os
import sys
import urllib.parse
import urllib.request
import webbrowser

CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
REDIRECT_URI = "http://localhost:8085"
SCOPES = "https://www.googleapis.com/auth/youtube.readonly"
TOKEN_FILE = "youtube_token.json"

auth_code = None


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        auth_code = params.get("code", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h1>Authorized! You can close this tab.</h1>")

    def log_message(self, format, *args):
        pass  # Suppress logs


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("ERROR: Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET env vars",
              file=sys.stderr)
        sys.exit(1)

    # Open browser for auth
    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        f"&response_type=code"
        f"&scope={urllib.parse.quote(SCOPES)}"
        f"&access_type=offline"
        f"&prompt=consent"
    )

    print("Opening browser for authorization...")
    webbrowser.open(auth_url)

    # Wait for the redirect
    server = http.server.HTTPServer(("localhost", 8085), Handler)
    server.handle_request()

    if not auth_code:
        print("ERROR: No authorization code received", file=sys.stderr)
        sys.exit(1)

    # Exchange code for tokens
    data = urllib.parse.urlencode({
        "code": auth_code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode()

    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    try:
        with urllib.request.urlopen(req) as resp:
            tokens = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print(f"ERROR: Token exchange failed ({e.code}): {error_body}", file=sys.stderr)
        sys.exit(1)

    token_data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": tokens["refresh_token"],
    }

    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)

    print(f"Saved refresh token to {TOKEN_FILE}")
    print("You can now run chat_poller.py")


if __name__ == "__main__":
    main()
