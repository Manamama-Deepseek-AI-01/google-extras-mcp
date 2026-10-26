#!/usr/bin/env python3
"""client_from_env.py — build a client JSON from exported OAuth env vars.

    python3 client_from_env.py --out ~/.config/google-extras-mcp/client.json

``auth_flow.py`` wants a file: a client JSON with an ``installed`` (or ``web``)
block. Plenty of setups never keep that file around, because the id and secret
live in an exported environment instead — a shared ``aliases.sh``, a container
image, a CI secret store. On a phone running Termux this is the common case: the
same aliases file drives every Google tool on the device, and re-downloading
client_secret_*.json just to authorise a second tool is busywork.

Recognised variables, in preference order:

    GOOGLE_OAUTH_CLIENT_ID      + GOOGLE_OAUTH_CLIENT_SECRET
    GOOGLE_CLIENT_ID            + GOOGLE_CLIENT_SECRET

Pass ``--pair client`` to prefer the second pair, for setups where the two are
different clients.

The written file contains a client secret: it is created 0600 inside a 0700
directory, and is never printed. Note that this is a *client* credential, not a
user credential — the user token still comes from ``auth_flow.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

PAIRS = {
    "oauth": ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"),
    "client": ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
}

DEFAULT_OUT = os.path.join(
    os.environ.get("GOOGLE_EXTRAS_CONFIG_DIR")
    or os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "google-extras-mcp"),
    "client.json",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write an OAuth client JSON from environment variables")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"file to write (default: {DEFAULT_OUT})")
    parser.add_argument("--pair", choices=sorted(PAIRS), default="oauth",
                        help="which variable pair to use, falling back to the other")
    parser.add_argument("--kind", choices=["installed", "web"], default="installed",
                        help="client block to write (default: installed)")
    args = parser.parse_args(argv)

    order = [args.pair] + [name for name in PAIRS if name != args.pair]
    for name in order:
        id_var, secret_var = PAIRS[name]
        client_id, client_secret = os.environ.get(id_var), os.environ.get(secret_var)
        if client_id and client_secret:
            used = name
            break
    else:
        pairs = " or ".join(f"{a}/{b}" for a, b in PAIRS.values())
        print(f"error: neither {pairs} is set in the environment", file=sys.stderr)
        return 1

    out = os.path.expanduser(args.out)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    os.chmod(os.path.dirname(out) or ".", 0o700)
    document = {
        args.kind: {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": AUTH_ENDPOINT,
            "token_uri": TOKEN_ENDPOINT,
            "redirect_uris": ["http://127.0.0.1"],
        }
    }
    descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(document, handle, indent=1)
    os.chmod(out, 0o600)

    # Print the project-number prefix only: enough to recognise which client this
    # is when you have several, not the whole id.
    project_hint = client_id.split("-")[0] if client_id.split("-")[0].isdigit() else client_id[:12]
    print(f"wrote {out} from {PAIRS[used][0]} ({args.kind} client, project {project_hint})")
    if args.kind == "web":
        print("note: a web client needs its registered redirect URI — pass it to "
              "auth_flow.py --redirect-uri", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
