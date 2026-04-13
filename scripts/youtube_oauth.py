#!/usr/bin/env python3
"""One-time OAuth2 flow to get a YouTube refresh token.

Usage:
  1. Go to https://console.cloud.google.com/apis/credentials
  2. Create an OAuth 2.0 Client ID (type: Web application)
     - Add http://localhost:8085 as an authorized redirect URI
  3. Download the JSON credentials as youtube_client_secret.json in
     the repo root (it's gitignored).
  4. Run: python3 scripts/youtube_oauth.py
     (or pass an explicit path: python3 scripts/youtube_oauth.py path/to.json)
  5. Open the URL it prints, authorize — the token is captured automatically
  6. Save the refresh token as YOUTUBE_REFRESH_TOKEN env var on Railway
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

DEFAULT_SECRET_FILE = "youtube_client_secret.json"
RAILWAY_GRAPHQL = "https://backboard.railway.com/graphql/v2"
RAILWAY_CONFIG = os.path.expanduser("~/.railway/config.json")

SCOPES = "https://www.googleapis.com/auth/youtube.force-ssl"
REDIRECT_URI = "http://localhost:8085"

auth_code = None
server_done = threading.Event()


class OAuthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)

        if "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Authorization successful!</h1><p>You can close this tab.</p>")
        else:
            error = params.get("error", ["unknown"])[0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(f"<h1>Authorization failed: {error}</h1>".encode())

        server_done.set()

    def log_message(self, format, *args):
        pass


def load_client_secret(path):
    """Read the standard Google Cloud client_secret.json file.

    The downloaded file wraps credentials under an "installed" or "web"
    key depending on client type — accept either.
    """
    with open(path) as f:
        data = json.load(f)
    for key in ("installed", "web"):
        if key in data:
            creds = data[key]
            return creds["client_id"], creds["client_secret"]
    raise ValueError(
        f"{path} is not a valid Google Cloud client secret file "
        f"(missing 'installed' or 'web' key)"
    )


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SECRET_FILE
    if not os.path.exists(path):
        print(f"ERROR: {path} not found.")
        print(f"Download your OAuth client credentials JSON from "
              f"https://console.cloud.google.com/apis/credentials")
        print(f"and save it as {DEFAULT_SECRET_FILE} in the repo root.")
        sys.exit(1)

    try:
        client_id, client_secret = load_client_secret(path)
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        print(f"ERROR reading {path}: {e}")
        sys.exit(1)
    print(f"Loaded OAuth client from {path}")

    # Start local server to catch redirect
    server = HTTPServer(("127.0.0.1", 8085), OAuthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Print auth URL
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    })
    print(f"\nOpen this URL in your browser:\n")
    print(f"https://accounts.google.com/o/oauth2/v2/auth?{params}\n")
    print("Waiting for authorization...")

    # Wait for the redirect
    server_done.wait(timeout=300)
    server.shutdown()

    if not auth_code:
        print("\nNo authorization code received.")
        sys.exit(1)

    print("\nGot authorization code, exchanging for tokens...")

    # Exchange code for tokens
    data = urllib.parse.urlencode({
        "code": auth_code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode()

    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        tokens = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"\nError: {e.read().decode()}")
        sys.exit(1)

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print(f"\nNo refresh token in response: {tokens}")
        sys.exit(1)

    print(f"\nSuccess! Your refresh token:\n")
    print(refresh_token)
    print()

    _offer_railway_update(refresh_token)


def _detect_railway_project():
    """Return (project_name, project_id, env_id, [(service_id, service_name)])
    for the currently linked Railway project, or None."""
    try:
        out = subprocess.check_output(
            ["railway", "status", "--json"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None

    project_id = data.get("id")
    project_name = data.get("name")
    if not project_id or not project_name:
        return None

    # Prefer the "production" environment, else the first one.
    envs = [e["node"] for e in data.get("environments", {}).get("edges", [])]
    prod = next((e for e in envs if e.get("name") == "production"), None)
    env = prod or (envs[0] if envs else None)
    if not env:
        return None

    services = [
        (si["node"]["serviceId"], si["node"]["serviceName"])
        for si in env.get("serviceInstances", {}).get("edges", [])
    ]
    if not services:
        return None
    return project_name, project_id, env["id"], services


def _railway_token():
    """Read the Railway access token from the CLI's config file."""
    try:
        with open(RAILWAY_CONFIG) as f:
            return json.load(f)["user"]["accessToken"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return None


def _railway_graphql(token, query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        RAILWAY_GRAPHQL,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    if data.get("errors"):
        raise RuntimeError(data["errors"])
    return data["data"]


def _offer_railway_update(refresh_token):
    detected = _detect_railway_project()
    if not detected:
        print("(Railway project not detected — run `railway link` to enable "
              "automatic env var updates, or set YOUTUBE_REFRESH_TOKEN "
              "manually on your services.)")
        return
    project_name, project_id, env_id, services = detected

    service_names = ", ".join(s[1] for s in services)
    prompt = (
        f"Would you like to update YOUTUBE_REFRESH_TOKEN on Railway project "
        f"'{project_name}' (services: {service_names})? [Y/n] "
    )
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        answer = "n"
    if answer not in ("", "y", "yes"):
        print("Skipped. Update the env var manually when ready.")
        return

    token = _railway_token()
    if not token:
        print(f"ERROR: couldn't read Railway access token from {RAILWAY_CONFIG}.")
        return

    mutation = (
        "mutation($p:String!,$e:String!,$s:String!,$v:String!){"
        " variableUpsert(input:{projectId:$p,environmentId:$e,"
        "serviceId:$s,name:\"YOUTUBE_REFRESH_TOKEN\",value:$v}) }"
    )
    for service_id, service_name in services:
        try:
            _railway_graphql(token, mutation, {
                "p": project_id, "e": env_id,
                "s": service_id, "v": refresh_token,
            })
            print(f"  ✓ {service_name}")
        except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError) as e:
            print(f"  ✗ {service_name}: {e}")

    print("\nDone. Railway will redeploy the affected services automatically.")


if __name__ == "__main__":
    main()
