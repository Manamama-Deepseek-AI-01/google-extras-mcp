#!/usr/bin/env python3
"""google-extras — a small read-only MCP server for three Google APIs that the
Google Workspace MCP server does not cover.

    Blogger   -> the account's blogs and their posts
    Books     -> My Library shelves and volumes, plus public catalogue search
    Classroom -> courses, announcements, coursework

Credentials
    OAuth   : a token file written by ``auth_flow.py`` (0600)
    API key : optional, only for anonymous public Books search

The server is deliberately read-only: the Blogger and Books scopes also permit
writes, so the absence of write tools here is a choice, not a limitation of the
grant. Worst case for an agent holding this server is a GET.

Tokens are never printed. Errors carry an HTTP status and a reason, never
credentials.

Run ``python3 server.py --check`` for a one-shot status report of every tool
without involving an MCP client.
"""

from __future__ import annotations

import argparse
import functools
import os
import sys
import time
from typing import Any

import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from mcp.server.mcpserver import MCPServer

VERSION = "0.1.0"

# --------------------------------------------------------------------- paths

CONFIG_DIR = os.environ.get("GOOGLE_EXTRAS_CONFIG_DIR") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "google-extras-mcp",
)

#: Written by ``auth_flow.py``. Overridable so the file can live elsewhere.
TOKEN_PATH = os.environ.get("GOOGLE_EXTRAS_TOKEN") or os.path.join(CONFIG_DIR, "credentials.json")

#: Optional single-line file holding an API key restricted to the free APIs.
KEY_PATH = os.environ.get("GOOGLE_EXTRAS_API_KEY_FILE") or os.path.join(CONFIG_DIR, "api-key")

#: Historical location used by one particular agent setup. Kept as a fallback so an
#: existing install keeps working when the environment is not passed through — MCP
#: clients spawn stdio children with a minimal environment by default.
LEGACY_TOKEN = os.path.expanduser("~/.config/opencode/secrets/google-extra-scopes.json")
LEGACY_KEY = os.path.expanduser("~/.config/opencode/secrets/google-free-apis-key")


def _resolve(primary: str, legacy: str) -> str:
    if os.path.exists(primary) or not os.path.exists(legacy):
        return primary
    print(f"google-extras: {primary} absent; using {legacy}", file=sys.stderr)
    return legacy


TOKEN_PATH = _resolve(TOKEN_PATH, LEGACY_TOKEN)
KEY_PATH = _resolve(KEY_PATH, LEGACY_KEY)

TIMEOUT = 30
BLOGGER = "https://www.googleapis.com/blogger/v3"
BOOKS = "https://www.googleapis.com/books/v1"
CLASSROOM = "https://classroom.googleapis.com/v1"

AUTH_HINT = (
    "No usable OAuth token. Run:  python3 auth_flow.py --client <oauth-client.json>  "
    "and open the printed URL. If the OAuth app is in 'Testing' publishing mode, "
    "Google expires refresh tokens after about 7 days — re-run the same command when "
    "that happens. For a non-interactive setup, set GOOGLE_EXTRAS_TOKEN to an "
    "authorised-user JSON file."
)

server = MCPServer(
    name="google-extras",
    version=VERSION,
    instructions=(
        "Read-only access to Blogger, Google Books (My Library) and Google Classroom. "
        "Complements the Google Workspace MCP server, which covers Gmail, Drive, "
        "Calendar, Docs, Sheets, Slides, Forms, Tasks, Chat, Contacts and Apps Script."
    ),
)

_creds: Credentials | None = None
_creds_stamp: float | None = None


# ---------------------------------------------------------------------- auth


def _credentials() -> Credentials:
    """Return usable credentials, re-reading the token file when it changes on disk.

    The mtime check is load-bearing: without it the process would pin whichever
    token it started with, and a re-minted file (the weekly re-consent, or a scope
    change) would be ignored until the process restarted.
    """
    global _creds, _creds_stamp
    try:
        stamp = os.path.getmtime(TOKEN_PATH)
    except OSError:
        raise RuntimeError(AUTH_HINT) from None

    if _creds is not None and _creds.valid and stamp == _creds_stamp:
        return _creds

    if _creds is None or stamp != _creds_stamp:
        try:
            _creds = Credentials.from_authorized_user_file(TOKEN_PATH)
        except Exception as exc:
            raise RuntimeError(
                f"token file unreadable ({type(exc).__name__}); {AUTH_HINT}"
            ) from None
        _creds_stamp = stamp

    if not _creds.valid:
        if not _creds.refresh_token:
            raise RuntimeError(f"no refresh token in {TOKEN_PATH}; {AUTH_HINT}")
        try:
            _creds.refresh(Request())
        except RefreshError as exc:
            _creds = None
            raise RuntimeError(f"refresh rejected ({exc}); {AUTH_HINT}") from None
    return _creds


def _api_key() -> str | None:
    try:
        with open(KEY_PATH) as handle:
            key = handle.read().strip()
        return key or None
    except OSError:
        return None


RETRYABLE = (429, 500, 502, 503, 504)
ATTEMPTS = 3


def _get(path: str, params: dict[str, Any] | None = None, *, auth: bool = True) -> dict[str, Any]:
    """GET a JSON document, or raise RuntimeError carrying status and reason only.

    Books and Classroom answer 503 often enough that a single attempt is not a
    test — the same query has been observed returning 200 and then 503 three
    seconds later — so transient statuses and read timeouts are retried with a
    widening pause.
    """
    headers = {"Authorization": f"Bearer {_credentials().token}"} if auth else {}
    response = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = requests.get(path, params=params, headers=headers, timeout=TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == ATTEMPTS:
                raise RuntimeError(
                    f"network error after {ATTEMPTS} attempts: {type(exc).__name__}"
                ) from None
            time.sleep(0.7 * attempt)
            continue
        except requests.RequestException as exc:
            raise RuntimeError(f"network error: {type(exc).__name__}: {exc}") from None
        if response.status_code not in RETRYABLE or attempt == ATTEMPTS:
            break
        time.sleep(0.7 * attempt)

    if response.status_code != 200:
        reason, message = "", ""
        try:
            error = response.json().get("error", {})
            reason = error.get("status", "") or ""
            message = (error.get("message") or "")[:200]
        except Exception:
            message = response.text[:200]
        raise RuntimeError(f"HTTP {response.status_code} {reason}: {message}".strip())
    try:
        return response.json()
    except ValueError:
        raise RuntimeError("response was not JSON") from None


def _guard(fn):
    """Return ``{"error": …}`` instead of raising, so the model can read and act.

    ``functools.wraps`` is load-bearing: MCPServer derives each tool's JSON schema
    from ``inspect.signature(fn)``. Without ``__wrapped__`` the schema degrades to
    ``(*args, **kwargs)``, and every call is rejected with "args: Field required"
    even though the catalogue looks perfect.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except RuntimeError as exc:
            return {"error": str(exc)}

    return wrapper


# -------------------------------------------------------------------- Blogger


@server.tool()
@_guard
def blogger_blogs() -> dict[str, Any]:
    """List the Blogger blogs this account can access: id, name, url, post count."""
    data = _get(f"{BLOGGER}/users/self/blogs")
    blogs = [
        {
            "id": blog.get("id"),
            "name": blog.get("name"),
            "url": blog.get("url"),
            "posts": (blog.get("posts") or {}).get("totalItems", 0),
            "updated": blog.get("updated"),
        }
        for blog in data.get("items", [])
    ]
    return {"count": len(blogs), "blogs": blogs}


@server.tool()
@_guard
def blogger_posts(blog_id: str, max_results: int = 10) -> dict[str, Any]:
    """List recent posts of one blog. Google caps max_results at 100."""
    data = _get(
        f"{BLOGGER}/blogs/{blog_id}/posts",
        {"maxResults": max(1, min(max_results, 100))},
    )
    posts = [
        {
            "id": post.get("id"),
            "title": post.get("title"),
            "published": post.get("published"),
            "url": post.get("url"),
            "labels": post.get("labels", []),
        }
        for post in data.get("items", [])
    ]
    return {
        "blog_id": blog_id,
        "count": len(posts),
        "total": data.get("totalItems"),
        "posts": posts,
    }


# ---------------------------------------------------------------------- Books


@server.tool()
@_guard
def books_shelves() -> dict[str, Any]:
    """List the account's Google Books shelves (Favorites, Reading now, ...)."""
    data = _get(f"{BOOKS}/mylibrary/bookshelves")
    shelves = [
        {
            "id": shelf.get("id"),
            "title": shelf.get("title"),
            "volumes": shelf.get("volumeCount") or 0,
        }
        for shelf in data.get("items", [])
    ]
    return {"count": len(shelves), "shelves": shelves}


@server.tool()
@_guard
def books_shelf_volumes(shelf_id: int, max_results: int = 20) -> dict[str, Any]:
    """List volumes on one shelf. Shelf ids are integers: 0 = Favorites, 3 = Reading now."""
    data = _get(
        f"{BOOKS}/mylibrary/bookshelves/{shelf_id}/volumes",
        {"maxResults": max(1, min(max_results, 40))},
    )
    volumes = []
    for item in data.get("items", []):
        info = item.get("volumeInfo", {})
        volumes.append(
            {
                "id": item.get("id"),
                "title": info.get("title"),
                "authors": info.get("authors", []),
                "publishedDate": info.get("publishedDate"),
            }
        )
    return {
        "shelf_id": shelf_id,
        "count": len(volumes),
        "total": data.get("totalItems"),
        "volumes": volumes,
    }


@server.tool()
@_guard
def books_search(query: str, max_results: int = 10) -> dict[str, Any]:
    """Search the public Google Books catalogue.

    Tries the free API key first if configured; falls back to OAuth (authenticated)
    if the key errors (such as 503 frontend blocks on certain restricted key targets).
    """
    params: dict[str, Any] = {"q": query, "maxResults": max(1, min(max_results, 40))}
    key = _api_key()
    data = None
    if key:
        params["key"] = key
        try:
            data = _get(f"{BOOKS}/volumes", params, auth=False)
        except RuntimeError:
            data = None
    if data is None:
        params.pop("key", None)
        data = _get(f"{BOOKS}/volumes", params, auth=True)

    results = [
        {
            "id": item.get("id"),
            "title": (item.get("volumeInfo") or {}).get("title"),
            "authors": (item.get("volumeInfo") or {}).get("authors", []),
            "link": (item.get("volumeInfo") or {}).get("infoLink"),
        }
        for item in data.get("items", [])
    ]
    return {
        "query": query,
        "count": len(results),
        "total": data.get("totalItems"),
        "results": results,
    }


# ------------------------------------------------------------------ Classroom


@server.tool()
@_guard
def classroom_courses() -> dict[str, Any]:
    """List the Google Classroom courses this account belongs to."""
    data = _get(f"{CLASSROOM}/courses", {"pageSize": 50})
    courses = [
        {
            "id": course.get("id"),
            "name": course.get("name"),
            "section": course.get("section"),
            "state": course.get("courseState"),
            "room": course.get("room"),
        }
        for course in data.get("courses", [])
    ]
    return {"count": len(courses), "courses": courses}


@server.tool()
@_guard
def classroom_announcements(course_id: str, page_size: int = 10) -> dict[str, Any]:
    """List announcements in a course, with truncated text bodies."""
    data = _get(
        f"{CLASSROOM}/courses/{course_id}/announcements",
        {"pageSize": max(1, min(page_size, 100))},
    )
    items = [
        {
            "id": item.get("id"),
            "createdTime": item.get("creationTime"),
            "state": item.get("state"),
            "text": (item.get("text") or "")[:300] or None,
        }
        for item in data.get("announcements", [])
    ]
    return {"course_id": course_id, "count": len(items), "announcements": items}


@server.tool()
@_guard
def classroom_coursework(course_id: str, page_size: int = 10) -> dict[str, Any]:
    """List coursework (assignments, questions) in a course."""
    data = _get(
        f"{CLASSROOM}/courses/{course_id}/courseWork",
        {"pageSize": max(1, min(page_size, 100))},
    )
    items = [
        {
            "id": item.get("id"),
            "title": item.get("title"),
            "type": item.get("workType"),
            "state": item.get("state"),
            "due": item.get("dueDate") or None,
            "maxPoints": item.get("maxPoints"),
        }
        for item in data.get("courseWork", [])
    ]
    return {"course_id": course_id, "count": len(items), "coursework": items}


# ----------------------------------------------------------------- self-check


def run_checks() -> int:
    """Call every tool once and report. Used by ``--check`` and by CI-ish sanity runs."""
    failures = 0
    print(f"token : {TOKEN_PATH} [{'present' if os.path.exists(TOKEN_PATH) else 'MISSING'}]")
    print(f"key   : {KEY_PATH} [{'present' if os.path.exists(KEY_PATH) else 'absent'}]")

    def run(label: str, fn, **kwargs) -> dict[str, Any]:
        nonlocal failures
        result = fn(**kwargs)
        if isinstance(result, dict) and result.get("error"):
            failures += 1
            print(f"  FAIL {label:<24} {str(result['error'])[:150]}")
            return {}
        counters = " ".join(
            f"{key}={result[key]}" for key in ("count", "total") if isinstance(result.get(key), int)
        )
        print(f"  ok   {label:<24} {counters}")
        return result

    blogs = run("blogger_blogs", blogger_blogs)
    if blogs.get("blogs"):
        run("blogger_posts", blogger_posts, blog_id=blogs["blogs"][0]["id"], max_results=2)

    shelves = run("books_shelves", books_shelves)
    if shelves.get("shelves"):
        run("books_shelf_volumes", books_shelf_volumes, shelf_id=shelves["shelves"][0]["id"], max_results=2)

    run("books_search", books_search, query="stanislaw lem", max_results=2)

    courses = run("classroom_courses", classroom_courses)
    if courses.get("courses"):
        course_id = courses["courses"][0]["id"]
        run("classroom_announcements", classroom_announcements, course_id=course_id)
        run("classroom_coursework", classroom_coursework, course_id=course_id)

    print(f"failures: {failures}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="google-extras",
        description="Read-only MCP server for Blogger, Google Books and Google Classroom.",
    )
    parser.add_argument("--check", action="store_true", help="run every tool once and report, then exit")
    parser.add_argument("--version", action="version", version=f"google-extras {VERSION}")
    args = parser.parse_args(argv)

    if args.check:
        return run_checks()

    # stdout belongs to the MCP protocol: diagnostics go to stderr.
    print(f"google-extras {VERSION}: starting stdio server", file=sys.stderr, flush=True)
    server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
