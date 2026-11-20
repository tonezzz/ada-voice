"""wiki_read — read a member-wiki page through board-api's read doors.

Ada co-owns the wiki (Education:/Reports: migrated 2026-11-08) but the
namespaces are Lockdown-protected — anonymous api.php reads get
accessdenied, so reads ride board-api's bot login, never direct. The
generic "/wiki/page mode=read" door the card spec'd does not exist yet
on board-api (the endpoint is write-only today); the tool still tries
it first so the day chaba lands it the door wins automatically, then
falls back to the namespace doors that DO exist:

  Education:*   -> /lesson {op:get, source:wiki}   (migrated lesson store)
  PrivateNews:* -> /wiki/news-review               (draft review read)

Everything else (Reports:, News/, templates, main space) reports the
gap honestly instead of pretending — card comms name the chaba
follow-up for a real mode=read + /wiki/search door.
"""

from __future__ import annotations

from typing import Any

from backend import board_client

_TITLE_MAX = 150
_MODES = ("read", "parse")

DECLARATION = {
    "name": "wiki_read",
    "description": (
        "Read a wiki page by exact title (Education: lessons, "
        "PrivateNews: drafts today). Unknown title? wiki_search first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Full page title, e.g. 'Education:Ada-Deploy-Flow'.",
            },
            "mode": {
                "type": "string",
                "description": "read (wikitext, default) or parse (rendered HTML).",
            },
        },
        "required": ["title"],
    },
}


def _content_of(result: dict[str, Any]) -> str:
    """Pull the page body out of whatever shape the door returned."""
    for key in ("wikitext", "content", "text", "html", "parse"):
        val = result.get(key)
        if isinstance(val, str) and val.strip():
            return val
    parse = result.get("parse")
    if isinstance(parse, dict):
        text = parse.get("text")
        if isinstance(text, dict):
            text = text.get("*")
        if isinstance(text, str) and text.strip():
            return text
    return ""


async def _try_page_door(title: str, mode: str) -> dict[str, Any] | None:
    """POST /wiki/page mode=read|parse — the spec'd generic door.

    Returns a normalized result on success, None on any failure so the
    caller can fall through to the namespace doors."""
    status, result = await board_client.post(
        "/wiki/page",
        {"mode": mode, "title": title, "from": "ada"}, timeout=30)
    if status <= 0:
        return {"ok": False, "error": result.get("error",
                                                 "board unavailable"),
                "_fatal": True}
    if status >= 400 or result.get("ok") is False:
        return None
    body = _content_of(result)
    if not body:
        return None
    return {"ok": True, "title": result.get("title") or title,
            "wikitext": body, "url": result.get("url", ""),
            "modified": result.get("modified", ""),
            "via": "wiki/page", "mode": mode}


async def _education_door(title: str) -> dict[str, Any] | None:
    status, result = await board_client.post(
        "/lesson",
        {"op": "get", "source": "wiki", "wiki_title": title,
         "from": "ada"}, timeout=60)
    if status <= 0:
        return {"ok": False, "error": result.get("error",
                                                 "board unavailable")}
    if status >= 400 or result.get("ok") is False:
        return None
    content = _content_of(result)
    if not content:
        return None
    return {"ok": True, "title": result.get("wiki_title") or title,
            "wikitext": content, "url": result.get("url", ""),
            "meta": result.get("meta") or {},
            "modified": result.get("modified", ""),
            "via": "lesson", "mode": "read"}


async def _private_news_door(title: str) -> dict[str, Any] | None:
    status, result = await board_client.post(
        "/wiki/news-review",
        {"wiki_title": title, "from": "ada"}, timeout=60)
    if status <= 0:
        return {"ok": False, "error": result.get("error",
                                                 "board unavailable")}
    if status >= 400 or result.get("ok") is False:
        return None
    content = _content_of(result)
    if not content:
        return None
    return {"ok": True, "title": result.get("wiki_title") or title,
            "wikitext": content,
            "revid": result.get("revid"),
            "via": "news-review", "mode": "read"}


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    title = str(args.get("title") or args.get("wiki_title") or "").strip()
    if not title or "\n" in title or "\r" in title or len(title) > _TITLE_MAX:
        return {"ok": False,
                "error": "title is required (one line, <=150 chars) — "
                         "e.g. 'Education:Ada-Deploy-Flow'"}
    mode = str(args.get("mode") or "read").strip().lower()
    if mode not in _MODES:
        return {"ok": False, "error": "mode must be read|parse"}

    attempted = []
    got = await _try_page_door(title, mode)
    if got and got.get("ok"):
        return got
    if got and got.get("_fatal"):
        return {"ok": False, "error": got["error"]}
    attempted.append("wiki/page")

    door = None
    if title.startswith("Education:"):
        door = _education_door
    elif title.startswith("PrivateNews:"):
        door = _private_news_door
    if door is not None:
        attempted.append(door.__name__)
        got = await door(title)
        if got:
            return got if got.get("ok") else {"ok": False,
                                              "error": got["error"]}

    covered = "Education:*, PrivateNews:*"
    if door is None:
        return {"ok": False,
                "error": (f"no board-api read door covers this title — "
                          f"readable today: {covered}. Reports:/News:/"
                          f"main space need the chaba mode=read door.")}
    return {"ok": False,
            "error": f"page not found or unreadable via {'/'.join(attempted)}: "
                     f"{title} — check the exact title with wiki_search"}
