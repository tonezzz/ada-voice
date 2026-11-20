"""wiki_search — recall across the member wiki without wiki creds.

board-api has no /wiki/search door yet (the card spec allowed for it),
so recall fans out over the doors and mirrors that DO exist:

  /wiki/search (POST)        — spec'd door; wins automatically once
                               chaba lands it (404s today -> fallback)
  mddb chaba-lessons         — vector search over the Education:
                               mirror every /lesson write lands in;
                               docs carry meta.wiki_title + url
  /lesson op=list source=wiki — Education: title index; catches exact
                               title hits vectors miss
  /report mode=search        — MDDB report dossiers (the Reports: side)

Hits dedupe on wiki title and return title/url/source/snippet/score —
feed a title straight into wiki_read to pull the page.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from backend import board_client

_QUERY_MAX = 200
_LIMIT_DEFAULT = 8
_LIMIT_MAX = 20
_SNIPPET_CHARS = 220
_WORD_RE = re.compile(r"\w+")

DECLARATION = {
    "name": "wiki_search",
    "description": (
        "Search the member wiki + report dossiers by meaning or title. "
        "Returns titles + urls — feed one to wiki_read to read the page."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Free-text search, e.g. 'deploy flow idc03'.",
            },
            "limit": {
                "type": "string",
                "description": "Max hits (default 8, cap 20).",
            },
        },
        "required": ["query"],
    },
}


def _meta_first(meta: dict | None, key: str) -> str:
    """MDDB metas store list values — unwrap the first."""
    val = (meta or {}).get(key)
    if isinstance(val, list):
        return str(val[0]) if val else ""
    return str(val) if val is not None else ""


def _mddb(runner: Any) -> Any | None:
    return getattr(getattr(runner, "context", None), "mddb", None)


def _norm_hit(title: str, url: str, source: str, score: float,
              snippet: str = "") -> dict[str, Any]:
    return {"title": title, "url": url, "source": source,
            "score": round(score, 4),
            "snippet": snippet[:_SNIPPET_CHARS]}


def _title_score(query: str, *fields: str) -> float:
    """Token overlap of query against title-ish fields -> 0..1."""
    q = set(_WORD_RE.findall(query.lower()))
    if not q:
        return 0.0
    hay = set(_WORD_RE.findall(" ".join(fields).lower()))
    return len(q & hay) / len(q)


async def _door_search(query: str, limit: int) -> dict[str, Any] | None:
    """POST /wiki/search — the spec'd generic door (not deployed yet).

    Normalized hits on success; None on 404/unsupported so the caller
    falls through to the composed search."""
    status, result = await board_client.post(
        "/wiki/search",
        {"query": query, "limit": limit, "from": "ada"}, timeout=30)
    if status <= 0:
        return {"ok": False, "error": result.get("error",
                                                 "board unavailable"),
                "_fatal": True}
    if status >= 400 or result.get("ok") is False:
        return None
    raw = result.get("results") or result.get("hits")
    if not isinstance(raw, list):
        return None
    hits = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        hits.append(_norm_hit(
            str(r.get("title") or r.get("wiki_title") or ""),
            str(r.get("url") or ""), "wiki",
            float(r.get("score") or 1.0),
            str(r.get("snippet") or r.get("excerpt") or "")))
    return {"ok": True, "query": query, "via": "wiki/search",
            "hits": hits, "count": len(hits)}


async def _mddb_lessons(runner: Any, query: str, limit: int) -> list:
    """chaba-lessons vector hits -> normalized rows (empty on outage)."""
    cli = _mddb(runner)
    if cli is None:
        return []
    try:
        docs = await cli.vector_search("chaba-lessons", query,
                                       limit=limit)
    except Exception:
        return []
    out = []
    for d in docs or []:
        meta = d.get("meta") or {}
        title = _meta_first(meta, "wiki_title") or \
            ("Education:" + _meta_first(meta, "topic")
             if _meta_first(meta, "topic") else str(d.get("key") or ""))
        out.append(_norm_hit(
            title, _meta_first(meta, "url"), "education",
            float(d.get("score") or 0.0),
            str(d.get("contentMd") or "")))
    return out


async def _lesson_titles(query: str) -> list:
    """/lesson op=list source=wiki — title-token matches over the
    Education: index (catches exact names vectors rank low)."""
    status, result = await board_client.post(
        "/lesson", {"op": "list", "source": "wiki", "from": "ada"},
        timeout=60)
    if status <= 0 or status >= 400 or result.get("ok") is False:
        return []
    out = []
    for row in result.get("lessons") or []:
        if not isinstance(row, dict):
            continue
        score = _title_score(query, str(row.get("wiki_title") or ""),
                             str(row.get("topic") or ""),
                             str(row.get("slug") or ""))
        if score <= 0:
            continue
        out.append(_norm_hit(
            str(row.get("wiki_title") or row.get("topic") or ""),
            str(row.get("url") or ""), "education", score))
    return out


async def _report_candidates(query: str, limit: int) -> list:
    """/report mode=search — MDDB report dossier candidates."""
    status, result = await board_client.post(
        "/report", {"mode": "search", "query": query, "from": "ada"},
        timeout=30)
    if status <= 0 or status >= 400 or result.get("ok") is False:
        return []
    out = []
    for c in (result.get("candidates") or [])[:limit]:
        if not isinstance(c, dict):
            continue
        slug = str(c.get("slug") or "")
        title = str(c.get("title") or slug)
        score = _title_score(query, title, slug)
        out.append(_norm_hit(title or slug, "", "report",
                             score or 0.3, slug))
    return out


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    query = str(args.get("query") or args.get("q") or "").strip()
    if not query or len(query) > _QUERY_MAX:
        return {"ok": False,
                "error": "query is required (<=200 chars)"}
    try:
        limit = int(str(args.get("limit") or _LIMIT_DEFAULT))
    except (TypeError, ValueError):
        limit = _LIMIT_DEFAULT
    limit = max(1, min(limit, _LIMIT_MAX))

    got = await _door_search(query, limit)
    if got and got.get("ok"):
        return got
    if got and got.get("_fatal"):
        return {"ok": False, "error": got["error"]}

    pools = await asyncio.gather(
        _mddb_lessons(runner, query, limit),
        _lesson_titles(query),
        _report_candidates(query, limit),
    )
    merged: dict[str, dict[str, Any]] = {}
    for hit in (h for pool in pools for h in pool):
        key = hit["title"].strip().lower()
        if not key:
            continue
        cur = merged.get(key)
        if cur is None:
            merged[key] = hit
        elif hit["score"] > cur["score"]:
            if not hit["snippet"] and cur["snippet"]:
                hit["snippet"] = cur["snippet"]
            merged[key] = hit
        elif not cur["snippet"] and hit["snippet"]:
            cur["snippet"] = hit["snippet"]
    hits = sorted(merged.values(), key=lambda h: h["score"],
                  reverse=True)[:limit]
    return {"ok": True, "query": query, "via": "fallback",
            "hits": hits, "count": len(hits),
            "note": ("searched chaba-lessons mirror + Education: index "
                     "+ report dossiers — board-api /wiki/search would "
                     "cover Reports:/News:/templates too")}
