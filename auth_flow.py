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

    # a "web" client, or any client with one fixed registered redirect URI:
    python3 auth_flow.py --client ~/client.json \
        --redirect-uri http://localhost:8000/oauth2callback

Then open the printed URL. The token is written to the config directory with
mode 0600, and the server picks it up on its next call (it re-reads the file
when the file's mtime changes, so no restart is needed).

Notes
    * Create the OAuth client as an **installed application** (Desktop) in a
      Google Cloud project, with the APIs you need enabled **in that project** —
      OAuth calls are quota'd against the client's project, not against whatever
      project your gcloud defaults point at.
    * No Desktop client to hand? A **web** client works too, but Google then
      compares the redirect URI as an exact string: you must pass the one
      registered for that client with ``--redirect-uri``, or the consent page
      answers ``Error 400: redirect_uri_mismatch`` before any account is even
      chosen. ``--login-hint user@example.com`` prefills the account and
      ``ACCOUNT:`` in the output tells you which account the token landed on.
    * Both ``127.0.0.1`` and ``::1`` are bound. On hosts where a connection to an
      unpublished loopback port hangs instead of being refused (Android/Termux,
      observed), a browser that prefers ``::1`` would otherwise sit there until
      it reported a timeout, and the callback would never arrive.
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
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"

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
    """Receives the one redirect Google makes, then stops the servers."""

    result: dict = {}
    servers: list = []
    expected_path: str = "/"

    def do_GET(self):  # noqa: N802 — stdlib naming
        url = urllib.parse.urlparse(self.path)
        params = {
            key: values[0]
            for key, values in urllib.parse.parse_qs(url.query).items()
        }
        # Only the redirect URI (or a request carrying an OAuth result) ends the
        # wait. Browsers happily fire extra requests at the same origin —
        # /favicon.ico, speculative preconnects — and treating one of those as
        # "the callback" would throw away the authorisation that follows.
        if url.path != Catcher.expected_path and not ({"code", "error"} & params.keys()):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
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
        for server in Catcher.servers:
            threading.Thread(target=server.shutdown, daemon=True).start()

    def log_message(self, *args):
        pass


class Server6(http.server.ThreadingHTTPServer):
    """IPv6 twin of the IPv4 listener.

    ``address_family`` belongs on the *server* class. Setting it on a handler
    subclass looks plausible, runs without complaint, and quietly keeps binding
    IPv4 only — which is exactly the bug this class exists to prevent.
    """

    address_family = socket.AF_INET6
    allow_reuse_address = True


def bind_listeners(port: int) -> list:
    """Listen on 127.0.0.1 and ::1, both on the same port.

    A browser resolves ``localhost`` to whichever family it prefers. If only
    IPv4 is bound and it picks ``::1``, the connection may *hang* rather than be
    refused (Android/Termux does this), so the browser never falls back and the
    user sees "connection timed out" while the process waits for a callback
    that cannot arrive. IPv6 is therefore best-effort but not optional.
    """
    try:
        first = http.server.ThreadingHTTPServer(("127.0.0.1", port), Catcher)
    except OSError as exc:
        raise SystemExit(f"cannot bind 127.0.0.1:{port}: {exc}")
    servers = [first]
    actual = first.server_address[1]
    try:
        servers.append(Server6(("::1", actual), Catcher))
    except OSError as exc:
        print(
            f"warning: no IPv6 loopback listener ({exc}); a browser that prefers ::1 may time out",
            file=sys.stderr,
        )
    Catcher.servers = servers
    shown = ", ".join(f"{server.server_address[0]}:{server.server_address[1]}" for server in servers)
    print(f"listening on {shown}", flush=True)
    return servers


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
    parser.add_argument(
        "--redirect-uri",
        help="exact redirect URI to use, e.g. http://localhost:8000/oauth2callback. "
        "Required when the client is a 'web' type client, whose registered redirect "
        "Google compares as an exact string. Binds that host/port/path instead of an "
        "ephemeral 127.0.0.1 port.",
    )
    parser.add_argument("--login-hint", help="prefill an account, e.g. user@example.com")
    parser.add_argument("--wait", type=int, default=1800, help="seconds to wait for the callback")
    args = parser.parse_args(argv)

    scopes = args.scope or DEFAULT_SCOPES
    client = load_client(os.path.expanduser(args.client))

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)

    if args.redirect_uri:
        bits = urllib.parse.urlparse(args.redirect_uri)
        if bits.scheme != "http" or (bits.hostname or "") not in ("localhost", "127.0.0.1", "::1"):
            raise SystemExit("--redirect-uri must be an http loopback URI (localhost or 127.0.0.1)")
        servers = bind_listeners(bits.port or 80)
        redirect_uri = args.redirect_uri
        Catcher.expected_path = bits.path or "/"
    else:
        servers = bind_listeners(args.port)
        redirect_uri = f"http://127.0.0.1:{servers[0].server_address[1]}/"
        Catcher.expected_path = "/"

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
    if args.login_hint:
        query["login_hint"] = args.login_hint
    print(f"AUTH_URL: {AUTH_ENDPOINT}?{urllib.parse.urlencode(query, quote_via=urllib.parse.quote)}", flush=True)

    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    deadline = time.time() + args.wait
    while not Catcher.result and time.time() < deadline:
        time.sleep(0.4)
    for server in servers:
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
    # Which account did this land on? With several Google sessions in a browser,
    # the click can authorise a different account than intended, and the token
    # records no address. One call to userinfo answers it unambiguously.
    try:
        request = urllib.request.Request(
            USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {token['access_token']}"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            info = json.load(response)
        print(f"ACCOUNT: {info.get('email')}")
    except Exception as exc:  # noqa: BLE001 — purely informational
        print(f"(could not confirm the account: {exc})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
