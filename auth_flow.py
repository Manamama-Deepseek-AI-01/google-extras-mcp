#!/usr/bin/env python3
"""auth_flow.py — obtain a Google OAuth token for google-extras, stdlib only.

Deliberately not built on a general-purpose OAuth library. Two reasons, both
observed in practice:

1. A provider is allowed to answer with a *neighbouring* scope set. Google has
   returned ``classroom.student-submissions.*`` when asked for
   ``classroom.coursework.*``. One popular OAuth library treats that as a fatal
   error and throws away a perfectly good grant — after the single-use
   authorisation code has already been spent. This script stores exactly what
   the provider returns.
2. A loopback flow must outlive the tool call that started it. Keeping the whole
   thing in one small process with no framework makes "why did the callback not
   arrive" a question with three possible answers instead of ten.

Usage
    python3 auth_flow.py --client ~/Downloads/client_secret_XXXX.json

Then open the printed URL. The token is written to the config directory with
mode 0600, and the server picks it up on its next call (it re-reads the file
when the file's mtime changes, so no restart is needed).

Notes
    * Create the OAuth client as an **installed application** (Desktop) in a
      Google Cloud project, with the APIs you need enabled **in that project** —
      OAuth calls are quota'd against the client's project, not against whatever
      project your gcloud defaults point at.
    * While the OAuth app is in "Testing" publishing mode, Google expires
      refresh tokens after about 7 days. Re-run this script when that happens.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

#: Scopes the MCP server's tools need. Trim this list if you only want some of
#: them — an unused scope is still a consent screen line and still a liability.
DEFAULT_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/blogger",                       # blogger_*
    "https://www.googleapis.com/auth/books",                          # books_shelves, books_shelf_volumes
    "https://www.googleapis.com/auth/classroom.courses.readonly",     # classroom_courses
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.students.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
]

DEFAULT_OUT = os.path.join(
    os.environ.get("GOOGLE_EXTRAS_CONFIG_DIR")
    or os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "google-extras-mcp"),
    "credentials.json",
)


class Catcher(http.server.BaseHTTPRequestHandler):
    """Receives the one redirect Google makes, then stops the server."""

    result: dict = {}

    def do_GET(self):  # noqa: N802 — stdlib naming
        params = {
            key: values[0]
            for key, values in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()
        }
        Catcher.result = params
        ok = "code" in params
        body = (
            b"Authorisation received. Close this tab and return to the terminal."
            if ok
            else f"Authorisation failed: {params.get('error', 'unknown')}".encode()
        )
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, *args):
        pass


def load_client(path: str) -> dict:
    with open(path) as handle:
        data = json.load(handle)
    if "installed" in data:
        return data["installed"]
    if "web" in data:
        print(
            "warning: this is a 'web' client. Loopback redirect URIs must be registered "
            "for it, and Google requires them to match exactly.",
            file=sys.stderr,
        )
        return data["web"]
    raise SystemExit(f"{path}: neither an 'installed' nor a 'web' client JSON")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="auth_flow.py", description=__doc__.splitlines()[0])
    parser.add_argument("--client", required=True, help="OAuth client JSON downloaded from the Cloud Console")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"token file to write (default: {DEFAULT_OUT})")
    parser.add_argument("--scope", action="append", help="add a scope; repeatable; replaces the defaults if given")
    parser.add_argument("--prompt", default="consent", choices=["none", "consent", "select_account"],
                        help="'none' attempts a silent flow when a grant already exists")
    parser.add_argument("--port", type=int, default=0, help="fixed loopback port (default: ephemeral)")
    parser.add_argument("--wait", type=int, default=1800, help="seconds to wait for the callback")
    args = parser.parse_args(argv)

    scopes = args.scope or DEFAULT_SCOPES
    client = load_client(os.path.expanduser(args.client))

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Catcher)
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/"

    query = {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "access_type": "offline",          # ask for a refresh token
        "include_granted_scopes": "true",  # keep scopes already granted to this client
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": args.prompt,
    }
    print(f"AUTH_URL: {AUTH_ENDPOINT}?{urllib.parse.urlencode(query, quote_via=urllib.parse.quote)}", flush=True)

    threading.Thread(target=server.serve_forever, daemon=True).start()
    deadline = time.time() + args.wait
    while not Catcher.result and time.time() < deadline:
        time.sleep(0.4)
    server.shutdown()

    params = Catcher.result
    if not params:
        print("TIMEOUT: no callback arrived", file=sys.stderr)
        return 2
    if "error" in params:
        print(f"OAUTH_ERROR: {params['error']} — {params.get('error_description', '')}", file=sys.stderr)
        return 3
    if params.get("state") != state:
        print("STATE_MISMATCH: refusing the callback", file=sys.stderr)
        return 4

    form = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": params["code"],
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
    ).encode()
    request = urllib.request.Request(
        TOKEN_ENDPOINT, data=form, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            token = json.load(response)
    except urllib.error.HTTPError as exc:
        print(f"TOKEN_EXCHANGE_FAILED: HTTP {exc.code} {exc.read().decode('utf-8', 'replace')[:300]}", file=sys.stderr)
        return 5

    expiry = datetime.now(timezone.utc) + timedelta(seconds=int(token.get("expires_in", 3600)))
    record = {
        "token": token["access_token"],
        "refresh_token": token.get("refresh_token"),
        "token_uri": TOKEN_ENDPOINT,
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "scopes": sorted((token.get("scope") or "").split()),
        "universe_domain": "googleapis.com",
        "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    }
    if not record["refresh_token"]:
        # A silent flow can omit it; losing the long-lived half would turn a
        # working credential into one that dies within the hour.
        try:
            with open(os.path.expanduser(args.out)) as handle:
                record["refresh_token"] = json.load(handle).get("refresh_token")
            print("note: no refresh_token returned; reused the existing one", file=sys.stderr)
        except Exception:
            print("warning: no refresh_token returned and none to reuse", file=sys.stderr)

    out = os.path.expanduser(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(record, handle, indent=1)
    os.chmod(out, 0o600)

    print(f"TOKEN_SAVED {out}")
    print(f"granted {len(record['scopes'])} scopes:")
    for scope in record["scopes"]:
        print("  ", scope.split("/auth/")[-1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
