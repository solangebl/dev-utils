#!/usr/bin/env python3
"""Set environment variables on one or more Netlify projects (sites).

Authenticates through a Netlify OAuth application. The client id and secret
are prompted for at runtime and never stored.

Usage:
    ./netlify_env.py --sites SITE_ID_1,SITE_ID_2 --env ENV1=value1 --env ENV2=value2 [--rebuild]

The OAuth application must list http://localhost:<port>/callback as its
redirect URI (default port: 8888).
"""

import argparse
import getpass
import http.server
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

AUTHORIZE_URL = "https://app.netlify.com/authorize"
TOKEN_URL = "https://api.netlify.com/oauth/token"
API_URL = "https://api.netlify.com/api/v1"
AUTH_TIMEOUT = 180  # seconds to wait for the browser redirect


class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status


def parse_env_pair(raw):
    key, sep, value = raw.partition("=")
    if not sep or not key.strip():
        raise argparse.ArgumentTypeError(f"invalid env pair: {raw!r} (expected KEY=value)")
    return key.strip(), value


def parse_sites(raw):
    sites = [s.strip() for s in raw.split(",") if s.strip()]
    if not sites:
        raise argparse.ArgumentTypeError("no site ids given")
    return sites


def wait_for_auth_code(port, expected_state):
    """Run a one-shot local server that captures the OAuth redirect."""
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            params = urllib.parse.parse_qs(url.query)
            result["code"] = params.get("code", [None])[0]
            result["state"] = params.get("state", [None])[0]
            result["error"] = params.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Authorization received. You can close this tab.")

        def log_message(self, *args):
            pass

    deadline = time.monotonic() + AUTH_TIMEOUT
    with http.server.HTTPServer(("localhost", port), Handler) as server:
        server.timeout = 1  # wake up regularly to check the deadline
        while "code" not in result:
            if time.monotonic() > deadline:
                sys.exit(f"Timed out after {AUTH_TIMEOUT}s waiting for authorization. "
                         "If Netlify showed an error (e.g. redirect URI mismatch), fix it and re-run.")
            server.handle_request()

    if result["error"]:
        sys.exit(f"Authorization failed: {result['error']}")
    if result["state"] != expected_state:
        sys.exit("Authorization failed: state mismatch")
    if not result["code"]:
        sys.exit("Authorization failed: no code returned")
    return result["code"]


def get_access_token(client_id, client_secret, port):
    redirect_uri = f"http://localhost:{port}/callback"
    state = secrets.token_urlsafe(16)
    authorize = AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    })
    print(f"Redirect URI (must match the OAuth app exactly): {redirect_uri}")
    print(f"Opening browser to authorize. If it doesn't open, visit:\n  {authorize}\n")
    print(f"Waiting up to {AUTH_TIMEOUT}s for authorization (Ctrl+C to abort)...")
    webbrowser.open(authorize)
    code = wait_for_auth_code(port, state)

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)["access_token"]
    except urllib.error.HTTPError as e:
        sys.exit(f"Token exchange failed: HTTP {e.code}: {e.read().decode()}")


def api(token, method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API_URL + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "dev-utils/netlify_env")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode()) from None


def set_env_var(token, account_id, site_id, key, value):
    """Create the variable, or replace its value (in all deploy contexts) if it exists."""
    q = urllib.parse.urlencode({"site_id": site_id})
    key_path = f"/accounts/{account_id}/env/{urllib.parse.quote(key, safe='')}?{q}"
    try:
        existing = api(token, "GET", key_path)
    except ApiError as e:
        if e.status != 404:
            raise
        existing = None

    values = [{"context": "all", "value": value}]
    if existing is None:
        api(token, "POST", f"/accounts/{account_id}/env?{q}", [{"key": key, "values": values}])
        return "created"

    payload = {"key": key, "values": values}
    if existing.get("scopes"):
        payload["scopes"] = existing["scopes"]
    if existing.get("is_secret"):
        payload["is_secret"] = True
    api(token, "PUT", key_path, payload)
    return "updated"


def main():
    parser = argparse.ArgumentParser(description="Set env variables on Netlify projects.")
    parser.add_argument("--sites", required=True, type=parse_sites,
                        help="comma-separated Netlify project (site) ids")
    parser.add_argument("--env", required=True, action="append", type=parse_env_pair,
                        help="KEY=value pair to set; repeat for multiple variables")
    parser.add_argument("--rebuild", action="store_true",
                        help="trigger a production build on every project where a variable was set")
    parser.add_argument("--port", type=int, default=8888,
                        help="local port for the OAuth callback (default: 8888)")
    args = parser.parse_args()

    client_id = input("Netlify OAuth client id: ").strip()
    client_secret = getpass.getpass("Netlify OAuth client secret: ").strip()
    if not client_id or not client_secret:
        sys.exit("Client id and secret are required.")

    token = get_access_token(client_id, client_secret, args.port)

    failures = 0
    for site_id in args.sites:
        try:
            site = api(token, "GET", f"/sites/{urllib.parse.quote(site_id, safe='')}")
        except ApiError as e:
            print(f"[{site_id}] could not load project: {e}")
            failures += 1
            continue
        name = site.get("name", site_id)
        changed = False
        for key, value in dict(args.env).items():
            try:
                action = set_env_var(token, site["account_id"], site["id"], key, value)
                print(f"[{name}] {key}: {action}")
                changed = True
            except ApiError as e:
                print(f"[{name}] {key}: FAILED ({e})")
                failures += 1

        if args.rebuild and changed:
            try:
                api(token, "POST", f"/sites/{site['id']}/builds")
                print(f"[{name}] rebuild: triggered")
            except ApiError as e:
                print(f"[{name}] rebuild: FAILED ({e})")
                failures += 1

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nAborted.")
