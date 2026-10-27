#!/usr/bin/env bash
# google-extras-mcp installer.
#
#   bash install.sh                                clone from GitHub
#   bash install.sh /path/to/existing-checkout     move that checkout into place (git clone, full history)
#
# Does the three scriptable steps and prints the one step that needs a human:
#   1. put the code in a durable place (~/.local/share/google-extras-mcp)
#   2. install dependencies
#   3. register the server with OpenCode
#   4. print the auth command — its consent screen needs a human at a browser.
set -euo pipefail

NAME="google-extras"
REPO_DEFAULT="https://github.com/Manamama-Deepseek-AI-01/google-extras-mcp"
TARGET="${GOOGLE_EXTRAS_HOME:-$HOME/.local/share/google-extras-mcp}"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"

# 1) Code to a durable place. ~/.local/share survives the rm -rf's that hit
#    Desktop/Downloads; the MCP client spawns server.py from here at every start.
if git -C "$TARGET" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "already installed: $TARGET  (git pull there to update)"
else
    mkdir -p "$(dirname "$TARGET")"
    git clone "${1:-$REPO_DEFAULT}" "$TARGET"
fi

# 2) Dependencies: three wheels, no gcloud / JDK / root.
python3 -m pip install -q -r "$TARGET/requirements.txt" \
    || python3 -m pip install -q --user -r "$TARGET/requirements.txt"

# 3) Register with OpenCode. NOTE: OpenCode reads MCP config at startup only —
#    no hot reload — so (re)start the client afterwards.
if grep -qs "\"$NAME\"" "$CONF_DIR/opencode.jsonc" "$CONF_DIR/opencode.json"; then
    echo "already registered in the OpenCode config"
elif command -v opencode >/dev/null 2>&1; then
    opencode mcp add "$NAME" --global -- python3 "$TARGET/server.py" \
        || echo "CLI add failed — add the block by hand (README: 'Registration in OpenCode')."
else
    echo "opencode CLI not found — register by hand (README: 'Registration in OpenCode')."
fi

# 4) The step no script can do for you: a human must consent in a browser.
cat <<EOF

Installed. Remaining step needs a human at a browser on THIS device:

  python3 "$TARGET/auth_flow.py" --client <your-oauth-client.json>

It prints AUTH_URL and waits; open that URL in a browser (Termux: termux-open "\$AUTH_URL")
and approve the consent screens. The token lands in ~/.config/google-extras-mcp/credentials.json.

Verify:  python3 "$TARGET/server.py" --check    # expect: failures: 0
EOF