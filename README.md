# google-extras-mcp

A read-only Model Context Protocol (MCP) server that extends the official Google Workspace MCP (`gws`) with three free APIs: **Blogger**, **Google Books (My Library)**, and **Google Classroom**.

## Why this exists

The official [Google Workspace MCP]([url](https://github.com/taylorwilsdon/google_workspace_mcp/)) (`gws`) covers 119 tools across Gmail, Google Drive, Calendar, Docs, Sheets, Slides, Forms, Tasks, Chat, Contacts, and Apps Script. However, it does not cover Blogger, Books, or Classroom. 

Rather than widening the scope of the main `gws` credential — which would mix broad Workspace permissions into a single long-lived token — **google-extras-mcp** runs as an independent local stdio server with its own minimal, disposable credential.

### Coverage at a glance

| Ecosystem area | Official `gws` MCP | `google-extras-mcp` |
| --- | --- | --- |
| **Email, Drive, Docs, Calendar, Chat, Tasks, Contacts, Script** | ✅ Yes (119 tools) | — |
| **Blogger** (blogs & posts) | — | ✅ Yes |
| **Google Books** (My Library & public search) | — | ✅ Yes |
| **Google Classroom** (courses, announcements, coursework) | — | ✅ Yes |

## Design Principles

1. **Strictly Read-Only:** The Blogger and Books OAuth scopes also permit writes, but this server exposes **zero** write tools. An agent holding this server can only execute HTTP `GET` requests.
2. **Independent Failure Domain:** Runs as a separate process with its own credential file (`credentials.json`), ensuring `gws` remains untouched and untestable-by-accident.
3. **Mtime Auto-Reload:** Re-reads the token file from disk whenever its modification time changes, so re-consents or scope updates take effect without restarting the MCP client.
4. **Upstream Fallback:** For Books catalogue search, it attempts an unauthenticated public query with an optional API key first, falling back transparently to the user's OAuth token if Google's frontend blocks the key with a `503`.

---

## Installation

Two plain steps: put the code somewhere **durable**, then register it with the client.

**Where the code lives: `$HOME/.local/share/google-extras-mcp`.**
Downloads and Desktop folders are disposable — assume they get `rm -rf`-ed. A server the client spawns at every startup must live somewhere that survives that. Clone with git (full history), so updates are a `git pull` in place.

**Scripted (recommended):**

```bash
bash install.sh                                   # clones from GitHub to ~/.local/share/google-extras-mcp
bash /old/location/google-extras-mcp/install.sh   # or: move an existing checkout there (git clone, full)
```

The script installs dependencies, registers the server with OpenCode, and prints the remaining (human) auth step. It is safe to re-run.

**Manual equivalent:**

```bash
git clone https://github.com/Manamama-Deepseek-AI-01/google-extras-mcp.git ~/.local/share/google-extras-mcp
python3 -m pip install -r ~/.local/share/google-extras-mcp/requirements.txt
# registration: next section
```

Termux / Android works too, with no extra system packages — `python3` plus those three wheels is the whole requirement. Nothing here needs `gcloud`, a JDK, or root; the auth script is stdlib-only. (If your Python refuses system-wide installs, add `--user` to the pip line.)

---

## Credentials & Authentication

Because Google's `gcloud` command-line OAuth client is allowlisted narrowly and refuses sensitive scopes like Blogger or Classroom, you authenticate using your own **Desktop / Installed Application** OAuth client from the Google Cloud Console.

### 1. Obtain an OAuth Client
1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create or select a project where you have enabled:
   * Blogger API (`blogger.googleapis.com`)
   * Google Books API (`books.googleapis.com`)
   * Google Classroom API (`classroom.googleapis.com`)
3. Configure the **OAuth consent screen** (Internal or External / Testing).
4. Create **Credentials** → **OAuth client ID** → Application type: **Desktop app**.
5. Download the client secret JSON file.

Already have a Desktop-type `client_secret_*.json` on this machine — e.g. the one another local MCP uses (`~/.config/gws/client_secret.json`)? Reuse it: the OAuth *client* is only an app identity; this flow mints its own independent token. The APIs from step 2 must be enabled in that client's project.

### 2. Run the Auth Flow — who does what

```bash
python3 ~/.local/share/google-extras-mcp/auth_flow.py --client <your-oauth-client.json>
```

The division of labour, so nobody improvises:

1. **The command** (you, or your agent) prints `AUTH_URL: https://accounts.google.com/...`, binds loopback (`listening on 127.0.0.1:PORT, ::1:PORT`) and **waits** — nothing proceeds until a browser comes back.
2. **A human opens `AUTH_URL` and approves the consent screens** — account choice, scope list, and in Testing mode the "Google hasn't verified this app → Continue" dance. This part cannot be scripted: the browser must run **on the same device** as the flow, because Google redirects back to `127.0.0.1:PORT`.
3. **On a phone:** `termux-open "$AUTH_URL"` pops the default browser (Cromite, Chrome, …) for the human. It engages from the Termux app; from an *ssh session into* Termux it may silently not fire — then copy the URL into the browser by hand.
4. On success the script writes `~/.config/google-extras-mcp/credentials.json` (`0600`) and prints `ACCOUNT: <address>`.
5. Verify with the Self-Check (last section): expect `failures: 0`.

> **Note on Testing Mode:** If your OAuth consent screen is in *Testing* publishing mode, Google expires refresh tokens after 7 days. When that happens, simply re-run `auth_flow.py`.

### 3. No `client_secret_*.json` to hand? Build it from your environment

Some setups never keep the JSON file: the id and secret live in exported environment variables — a shared `aliases.sh` sourced on every machine, a container image, a CI secret store. `client_from_env.py` turns that into the file `auth_flow.py` expects:

```bash
python3 client_from_env.py --out ~/.config/google-extras-mcp/client.json
```

It reads `GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET`, falling back to `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` (override with `--pair client`). The file is written `0600` inside a `0700` directory and the secret is never printed — only the project number, so you can tell two clients apart.

### 4. Web clients, phones, and the ``::1`` trap

A **Desktop/installed** client accepts any loopback port, so the default flow just works. A **web** client does not: Google compares its redirect URI as an exact string, and a mismatch is answered with `Error 400: redirect_uri_mismatch` *before* an account is even chosen. Pass the registered URI verbatim:

```bash
python3 auth_flow.py --client ~/.config/google-extras-mcp/client.json \
    --redirect-uri http://localhost:8000/oauth2callback
```

Two details that cost real time to diagnose, so they are handled in the script:

* **Both loopback families are bound.** `localhost` may resolve to `127.0.0.1` or `::1`. On hosts where a connection to an unpublished loopback port *hangs* instead of being refused — Android/Termux does this — a browser that picks `::1` waits until it reports "The connection timed out", never falls back to IPv4, and the callback never arrives. The script prints what it bound, e.g. `listening on 127.0.0.1:8000, ::1:8000`. If that line shows only IPv4, expect exactly that timeout.
* **Stray requests do not end the wait.** Browsers fire `/favicon.ico` and speculative connections at the same origin; only the registered path (or a request carrying `code`/`error`) counts as the callback. Everything else gets a `404`.

`--login-hint user@example.com` prefills the account when the browser has several Google sessions open, and the final `ACCOUNT:` line reports which account the token actually belongs to — the token file itself records no address.

### Why not `gcloud auth login`?

Because it cannot ask for these scopes, and on a phone it cannot run at all:

* `gcloud` ships with **Google's own** OAuth client, which is allowlisted to Cloud scopes. Requesting `…/auth/blogger` or `…/auth/classroom.*` through it fails with `invalid_scope`. The allowlist lives on Google's side; no flag changes it. This is the whole reason this repo has its own auth script.
* The Cloud SDK is glibc-linked, so it does not run under Termux's bionic libc (no official arm64 Termux package exists).
* The PyPI package named `gcloud` is the long-retired *API client library* (`GoogleCloudPlatform/gcloud-python`), not the CLI: it installs no `gcloud` executable.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Error 400: redirect_uri_mismatch` | Web client, redirect compared exactly | Pass the client's registered URI with `--redirect-uri` |
| Browser: "The connection timed out" on `localhost:<port>`, terminal still waiting | Only IPv4 bound, browser tried `::1` | Update to this version; check the `listening on …` line names `::1` too |
| `invalid_scope` at the consent page | The API is not enabled in the client's own project | Enable Blogger / Books / Classroom in that project |
| Token works, then fails after ~7 days | Consent screen in *Testing* | Re-run the flow, or publish the app (unverified is fine) |
| `granted` scopes differ from what you asked | Google substituted a neighbouring scope set — e.g. `classroom.student-submissions.*` for `classroom.coursework.*` | Expected; the script stores what was actually granted |
| Account authorised is not the one you wanted | Several Google sessions in the browser | `--login-hint`, then check the `ACCOUNT:` line |
| `termux-open` did nothing — browser never opened | Flow was started over an ssh session *into* Termux; no app context to engage the browser | Run the flow from the Termux app itself, or paste `AUTH_URL` into the browser by hand |
| Server absent from `/mcps` and `opencode mcp list` after registering | OpenCode reads MCP config only at startup — no hot reload | Restart OpenCode, or start a new session |

---

## Tool Catalogue (8 Read-Only Tools)

| Tool Name | Description | Key Parameters |
|---|---|---|
| `blogger_blogs` | List Blogger blogs accessible to the account | — |
| `blogger_posts` | List recent posts of a specific blog | `blog_id`, `max_results` |
| `books_shelves` | List My Library shelves (Favorites, Reading Now, etc.) | — |
| `books_shelf_volumes` | List volumes on a specific shelf | `shelf_id` (integer), `max_results` |
| `books_search` | Search the public Google Books catalogue | `query`, `max_results` |
| `classroom_courses` | List Google Classroom courses | — |
| `classroom_announcements` | List announcements in a course | `course_id`, `page_size` |
| `classroom_coursework` | List coursework (assignments/questions) in a course | `course_id`, `page_size` |

---

## Registration in OpenCode (or any MCP Client)

One command — the CLI edits the config itself (and honours JSONC comments):

```bash
opencode mcp add google-extras --global -- python3 "$HOME/.local/share/google-extras-mcp/server.py"
```

Or by hand, in the global config (`~/.config/opencode/opencode.jsonc`), under `"mcp"` → `"servers"`:

```jsonc
"mcp": {
  "servers": {
    "google-extras": {
      "type": "local",
      "command": [
        "python3",
        "/home/you/.local/share/google-extras-mcp/server.py"
      ],
      "enabled": true
    }
  }
}
```

* `python3` stays **bare** — the client resolves it on the PATH of the user running it. Do not hard-code `/usr/bin/python3`; that is one machine's path, not a universal one.
* The server path must be **absolute**: JSONC does not expand `~`.
* **No hot reload.** OpenCode reads MCP config at startup only. Restart the client (or start a new session) after registering; `/mcps` then shows `google-extras` connected, and `opencode mcp list` lists it.

### Self-Check

You can verify the entire server and its credentials without starting an MCP client — run it after install and after every re-auth:

```bash
python3 ~/.local/share/google-extras-mcp/server.py --check    # expect: failures: 0
```

And the rule that saves the most time: **the check's failure text is the manual.** When a tool line reads

```
FAIL blogger_blogs   No usable OAuth token. Run:  python3 auth_flow.py --client <oauth-client.json>  and open the printed URL. …
```

that line *is* the next step — run exactly that command, hand the printed `AUTH_URL` to a human, and let the browser consent. Don't improvise around it.

---

## Roadmap & Upstream PR Plan

The mid-term goal is to fold these capabilities upstream into the official Google Workspace MCP (`gws`). That has been deferred because:
1. Upstream maintainers maintain strict tool boundaries and scope discipline.
2. Classroom and Blogger require distinct consent screen configurations that not all Workspace users want or need in a default toolset.

Once edge cases and scope substitution behaviours (such as Google returning `student-submissions` scopes when asked for `coursework` scopes) are fully characterised, a modular PR or extension mechanism will be proposed upstream.

---

## License

MIT License — see [LICENSE](./LICENSE) for details.
