#!/usr/bin/env python3
"""Read-only Ada control-plane MCP bridge for the DevNest OpenCode project."""
from __future__ import annotations

import concurrent.futures
import functools
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

BOARD_API = os.environ.get(
    "ADA_BOARD_API",
    "https://idc03.taila0626a.ts.net:10000",
).rstrip("/")
MDDB_CMS = os.environ.get("ADA_MDDB_CMS_URL", "http://100.102.134.91:11027/v1").rstrip("/")
MDDB_MEMORY = os.environ.get(
    "ADA_MEMORY_MDDB_URL",
    "http://100.102.134.91:11023/v1",
).rstrip("/")
try:
    WORKSPACE = Path.cwd()
except FileNotFoundError:
    # OpenChamber chat dirs can be deleted before the MCP child spawns;
    # Path.cwd() then raises. Fall back so the bridge still starts.
    WORKSPACE = Path.home()
ADOBE_STOCK_INDEX_FILES = (
    "campaigns.json",
    "assets.json",
    "rejection-patterns.json",
    "prompt-patterns.json",
)

def _resolve_bridge_version() -> str:
    """Identify which bridge copy is serving: env > VERSION file > git > content hash.

    Deploy can inject ADA_BRIDGE_VERSION or write .opencode/VERSION
    (e.g. `git rev-parse HEAD > .opencode/VERSION`). A repo checkout
    resolves `git describe --always --dirty`. Anything else falls back to
    a hash of this file so a stale copy still reports a different version.
    """
    env_version = os.environ.get("ADA_BRIDGE_VERSION", "").strip()
    if env_version:
        return env_version
    here = Path(__file__).resolve().parent
    try:
        file_version = (here / "VERSION").read_text(encoding="utf-8").splitlines()[0].strip()
        if file_version:
            return file_version
    except (OSError, IndexError):
        pass
    try:
        proc = subprocess.run(
            ["git", "describe", "--always", "--dirty", "--tags"],
            cwd=here, capture_output=True, text=True, timeout=5,
        )
        sha = proc.stdout.strip()
        if proc.returncode == 0 and sha:
            return f"git-{sha}"
    except Exception:
        pass
    try:
        digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:10]
        return f"dev-{digest}"
    except OSError:
        return "dev-unknown"


BRIDGE_VERSION = _resolve_bridge_version()


def _audit_dir() -> Path:
    """calls.jsonl location; ADA_CONTROL_AUDIT_DIR overrides for tests/debug."""
    override = os.environ.get("ADA_CONTROL_AUDIT_DIR", "").strip()
    return Path(override) if override else Path.home() / ".local" / "share" / "ada-control"


mcp = FastMCP("ada-control")
PENDING_NEXT_SELECTIONS: dict[str, tuple[str, float]] = {}

RISK_TERMS = (
    "deploy", "deployment", "production", "credential", "secret", "delete", "destructive",
    "database", "install", "systemd", "service", "origin", "restore",
    "timer", "rollout", "video", "render", "gpu", "mlx", "media",
        "rerender", "prune", "compact", "wp", "wordpress", "upsert",
)
NEGATION_RE = re.compile(
    r"\b(?:no|not|never|without|do\s+not|does\s+not|must\s+not|"
    r"blocked|prohibited|forbidden)\b[^.!?]{0,80}$",
    re.I,
)


def _positive_risk_terms(text: str) -> list[str]:
    """Find affirmative risk terms while ignoring nearby prohibitions."""
    found = []
    lower = text.lower()
    for term in RISK_TERMS:
        for match in re.finditer(rf"\b{re.escape(term)}\b", lower):
            prefix = lower[max(0, match.start() - 100):match.start()]
            if not NEGATION_RE.search(prefix):
                found.append(term)
                break
    return found


def _selection_gate(card_reference: str, selection_token: str) -> str | None:
    """Bind a next-card plan to a card that was actually offered in the menu."""
    now = time.monotonic()
    for ref, (_, expires) in list(PENDING_NEXT_SELECTIONS.items()):
        if expires <= now:
            PENDING_NEXT_SELECTIONS.pop(ref, None)
    ref = card_reference.strip().lower()
    pending = PENDING_NEXT_SELECTIONS.get(ref)
    if pending and selection_token != pending[0]:
        return "selection_token_required"
    if PENDING_NEXT_SELECTIONS and not pending:
        return "selection_not_in_current_menu"
    if pending:
        PENDING_NEXT_SELECTIONS.pop(ref, None)
    return None


def _get_json(path: str, timeout: float = 15) -> Any:
    req = urllib.request.Request(f"{BOARD_API}{path}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def _post_json(path: str, payload: dict[str, Any], timeout: float = 3) -> Any:
    req = urllib.request.Request(
        f"{BOARD_API}{path}",
        data=json.dumps(payload).encode(),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def _mddb_post(path: str, payload: dict[str, Any], timeout: float = 5) -> Any:
    req = urllib.request.Request(
        f"{MDDB_CMS}/{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def _memory_mddb_post(path: str, payload: dict[str, Any], timeout: float = 5) -> Any:
    req = urllib.request.Request(
        f"{MDDB_MEMORY}/{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


_SNAPSHOT_TTL = 20.0
_snapshot_cache: dict[str, tuple[float, Any]] = {}


def _get_json_cached(path: str, ttl: float = _SNAPSHOT_TTL) -> Any:
    """Short-lived cache for board reads so one workflow reuses snapshots."""
    now = time.monotonic()
    hit = _snapshot_cache.get(path)
    if hit and hit[0] > now:
        return hit[1]
    for key, (expires, _) in list(_snapshot_cache.items()):
        if expires <= now:
            _snapshot_cache.pop(key, None)
    payload = _get_json(path)
    _snapshot_cache[path] = (now + ttl, payload)
    return payload


_CARD_PROJECTED_FIELDS = (
    "id,ref,title,column,priority,updated,brief,spec,action,requests,aliases"
)
_CARD_FULL_FIELDS = _CARD_PROJECTED_FIELDS + ",comms"


def _cards_payload(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("cards") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    return [c for c in rows if isinstance(c, dict)]


def _compact_cards() -> list[dict[str, Any]]:
    """Minimal card index (~200KB) for id/ref/title resolution."""
    return _cards_payload(_get_json_cached("/cards?compact=1&limit=1000"))


def _board_cards() -> list[dict[str, Any]]:
    """Projected all-card snapshot (~1MB, no comms) vs the 9MB full dump.

    Uses fields= rather than compact=2 because board-api's compact=2 tier
    drops action/requests/aliases, which find_card scoring, _pick_cards
    and preflight gates all need.
    """
    return _cards_payload(
        _get_json_cached(f"/cards?fields={_CARD_PROJECTED_FIELDS}&limit=1000")
    )


def _cards() -> list[dict[str, Any]]:
    payload = _get_json("/cards")
    if not isinstance(payload, dict) or not isinstance(payload.get("cards"), list):
        raise RuntimeError("board response did not contain a cards list")
    return [c for c in payload["cards"] if isinstance(c, dict)]


# Hydrated cards only ever read the last few comms (_safe_card tails to
# 5, context text to _CONTEXT_MAX_COMMS=5) — comms_tail keeps the wire
# payload small on chatty cards; board-api adds comms_total for free.
_HYDRATE_COMMS_TAIL = 8


def _fetch_card_full(card_id: Any) -> dict[str, Any] | None:
    """Fetch one card's safe fields with a bounded comms tail."""
    if card_id in (None, ""):
        return None
    path = (
        f"/cards?id={urllib.parse.quote(str(card_id), safe='')}"
        f"&fields={_CARD_FULL_FIELDS}&comms_tail={_HYDRATE_COMMS_TAIL}&limit=1"
    )
    rows = _cards_payload(_get_json_cached(path, ttl=60.0))
    return rows[0] if rows else None


def _card_action(card: dict[str, Any]) -> dict[str, Any]:
    action = card.get("action")
    return action if isinstance(action, dict) else {}


def _request_open(request: Any) -> bool:
    """Non-dict request rows count as open (conservative)."""
    if not isinstance(request, dict):
        return True
    return request.get("status") not in ("answered", "closed")


def _terms(text: str) -> set[str]:
    raw = re.findall(r"[a-z0-9][a-z0-9_-]{2,}", text.lower())
    terms = set(raw)
    for value in raw:
        terms.update(part for part in re.split(r"[-_]", value) if len(part) >= 3)
    return terms


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").replace("&nbsp;", " ")


def _meta_value(meta: dict[str, Any], name: str) -> Any:
    value = meta.get(name)
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _lesson_memory_mddb(query: str, deadline: float, limit: int = 3) -> list[dict[str, Any]]:
    """Read Education lessons from the fast MDDB chaba-lessons mirror.

    The CMS /search endpoint returns the mirrored docs with contentMd in a
    single call (~0.3s for the full corpus); relevance is ranked locally.
    """
    remaining = max(0.25, min(2.5, deadline - time.monotonic()))
    payload = _mddb_post(
        "search",
        {"collection": "chaba-lessons", "limit": 500},
        timeout=remaining,
    )
    if isinstance(payload, dict):
        payload = payload.get("result", payload.get("docs", []))
    if not isinstance(payload, list):
        return []
    q = _terms(query)
    ranked = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        head = " ".join(
            str(value or "")
            for value in (
                _meta_value(meta, "topic"),
                _meta_value(meta, "slug"),
                _meta_value(meta, "wiki_title"),
                _meta_value(meta, "title"),
                row.get("key"),
            )
        )
        body = str(row.get("contentMd") or row.get("content") or "")
        score = len(q & _terms(head)) * 3 + len(q & _terms(body[:4000]))
        if score:
            ranked.append((score, row, meta))
    hits = []
    for score, row, meta in sorted(
        ranked, key=lambda item: (-item[0], str(item[1].get("key") or ""))
    )[:limit]:
        hits.append({
            "source": "education",
            "storage": "mddb",
            "collection": "chaba-lessons",
            "key": row.get("key"),
            "slug": _meta_value(meta, "slug"),
            "topic": _meta_value(meta, "topic"),
            "wiki_title": _meta_value(meta, "wiki_title"),
            "url": _meta_value(meta, "url"),
            "level": _meta_value(meta, "level"),
            "score": score,
            "excerpt": str(row.get("contentMd") or row.get("content") or "")[:1200],
        })
    return hits


def _lesson_memory_board(query: str, deadline: float, limit: int = 3) -> list[dict[str, Any]]:
    """Fallback: read Education lessons via the slow WP-backed board-api."""
    remaining = max(0.25, min(1.5, deadline - time.monotonic()))
    payload = _post_json("/lesson", {"op": "list", "from": "devin"}, timeout=remaining)
    rows = payload.get("lessons", []) if isinstance(payload, dict) else []
    q = _terms(query)
    ranked = []
    for row in rows:
        text = " ".join(str(row.get(k) or "") for k in ("topic", "slug", "level"))
        score = len(q & _terms(text))
        if score:
            ranked.append((score, row))
    hits = []
    for score, row in sorted(ranked, key=lambda item: (-item[0], item[1].get("slug", "")))[:limit]:
        if time.monotonic() >= deadline:
            break
        try:
            remaining = max(0.25, min(0.75, deadline - time.monotonic()))
            detail = _post_json(
                "/lesson",
                {"op": "get", "slug": row["slug"], "from": "devin"},
                timeout=remaining,
            )
            body = _strip_html(detail.get("content", ""))
        except Exception as exc:
            body = f"lesson read failed: {exc}"
        hits.append({
            "source": "education",
            "storage": "board-api",
            "slug": row.get("slug"),
            "topic": row.get("topic"),
            "score": score,
            "excerpt": body[:1200],
        })
    return hits


def _lesson_memory(query: str, deadline: float, limit: int = 3) -> list[dict[str, Any]]:
    """Prefer the MDDB chaba-lessons mirror; fall back to board-api /lesson."""
    try:
        return _lesson_memory_mddb(query, deadline, limit)
    except Exception as mddb_exc:
        try:
            return _lesson_memory_board(query, deadline, limit)
        except Exception as board_exc:
            raise RuntimeError(f"mddb:{mddb_exc}; board-api:{board_exc}") from board_exc


def _devin_memory(query: str, deadline: float, limit: int = 3) -> list[dict[str, Any]]:
    roots = [Path(os.environ.get("ADA_DEVIN_SUMMARIES", Path.home() / ".local/share/devin/summaries")), Path.home() / ".local/share/devin/cli/summaries"]
    q = _terms(query)
    ranked = []
    for root in roots:
        if not root.exists():
            continue
        paths = sorted(
            root.glob("*.md"),
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )[:30]
        for path in paths:
            if time.monotonic() >= deadline:
                break
            try:
                body = path.read_text(errors="replace")[:12000]
            except OSError:
                continue
            score = len(q & _terms(body[:12000]))
            if score:
                ranked.append((score, path, body))
    return [{"source": "devin-session", "path": str(path), "score": score, "excerpt": body[:1200]}
            for score, path, body in sorted(ranked, key=lambda item: (-item[0], str(item[1])))[:limit]]


def _devin_mddb_memory(query: str, deadline: float, limit: int = 3) -> list[dict[str, Any]]:
    """Read normalized Devin summaries from the canonical MDDB collection."""
    remaining = max(0.25, min(2.0, deadline - time.monotonic()))
    payload = _memory_mddb_post(
        "search",
        {
            "collection": "ada-ha-bank-devin-tony",
            "query": query,
            "limit": limit,
        },
        timeout=remaining,
    )
    if not isinstance(payload, list):
        return []
    hits = []
    for row in payload[:limit]:
        if not isinstance(row, dict):
            continue
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        hits.append({
            "source": "devin-session",
            "storage": "mddb",
            "key": row.get("key"),
            "score": row.get("score"),
            "card_id": (meta.get("card_id") or [None])[0]
            if isinstance(meta.get("card_id"), list) else meta.get("card_id"),
            "workflow_id": (meta.get("workflow_id") or [None])[0]
            if isinstance(meta.get("workflow_id"), list) else meta.get("workflow_id"),
            "job_id": (meta.get("job_id") or [None])[0]
            if isinstance(meta.get("job_id"), list) else meta.get("job_id"),
            "freshness": meta.get("date") or meta.get("updated_at"),
            "excerpt": str(row.get("contentMd") or row.get("content") or "")[:1200],
        })
    return hits


def recall_memory(query: str, deadline: float | None = None) -> dict[str, Any]:
    """Read Education and canonical Devin summaries with local fallback."""
    errors = []
    deadline = deadline if deadline is not None else time.monotonic() + 4.5
    try:
        lessons = _lesson_memory(query, deadline)
    except Exception as exc:
        lessons = []
        errors.append(f"education:{exc}")
    try:
        sessions = _devin_mddb_memory(query, deadline)
        if len(sessions) < 3 and time.monotonic() < deadline:
            sessions.extend(_devin_memory(query, deadline, limit=3 - len(sessions)))
    except Exception as exc:
        errors.append(f"devin-mddb:{exc}")
        try:
            sessions = _devin_memory(query, deadline)
        except Exception as local_exc:
            sessions = []
            errors.append(f"devin-session:{local_exc}")
    return {"query": query, "lessons": lessons, "devin_sessions": sessions, "errors": errors}



def _excerpt_window(content: str, query: str, width: int = 1200) -> str:
    """Return ~width chars centered on the best query-term match.

    SSOT docs are long YAML documents whose answer blocks (e.g. a host's
    spec) sit far below the head, so excerpting content[:width] surfaces
    only metadata and forces agents to re-query. Score each line by query
    term overlap, window around the best line, and fall back to the head.
    """
    if len(content) <= width:
        return content
    terms = _terms(query)
    if not terms:
        return content[:width]
    best_off, best_score, offset = 0, 0, 0
    for line in content.split("\n"):
        low = line.lower()
        score = sum(1 for t in terms if t in low)
        if score > best_score:
            best_score, best_off = score, offset
        offset += len(line) + 1
    if best_score == 0:
        return content[:width]
    start = max(0, best_off - width // 3)
    nl = content.rfind("\n", 0, start)
    if nl != -1:
        start = nl + 1
    excerpt = content[start:start + width]
    prefix = "\u2026" if start > 0 else ""
    suffix = "\u2026" if start + width < len(content) else ""
    return prefix + excerpt + suffix


def _ssot_meta_first(meta: dict[str, Any], name: str) -> Any:
    value = meta.get(name)
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _ssot_hit(collection: str, doc: dict[str, Any], score: Any, content: str,
              query: str = "") -> dict[str, Any]:
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    return {
        "source": "ssot",
        "storage": "mddb",
        "collection": collection,
        "key": doc.get("key"),
        "path": _ssot_meta_first(meta, "original_path") or "",
        "title": _ssot_meta_first(meta, "title"),
        "score": score,
        "excerpt": _excerpt_window(content, query),
    }


def _ssot_search(query: str, deadline: float, limit: int = 3) -> tuple[list[dict[str, Any]], list[str]]:
    """Search canonical SSOT documents in MDDB; read-only and collection-allowlisted.

    Primary path is /vector-search for real relevance — CMS /search ignores
    `query` and returns the collection head. vector-search strips content,
    so each hit is hydrated with a single /get call. Falls back to /search
    per collection when vector-search fails.
    """
    hits: list[dict[str, Any]] = []
    errors: list[str] = []
    collections = ("infrastructure-ssot", "chaba-glossary")
    for collection in collections:
        remaining = max(0.25, min(3.0, deadline - time.monotonic()))
        if remaining <= 0.25:
            break
        docs: list[tuple[dict[str, Any], Any]] = []
        try:
            payload = _mddb_post(
                "vector-search",
                {
                    "collection": collection,
                    "query": query,
                    "top_k": limit * 2,
                    "retrieval_mode": "parent",
                    "include_content": False,
                },
                timeout=remaining,
            )
            results = payload.get("results") if isinstance(payload, dict) else None
            if isinstance(results, list):
                seen_keys: set[str] = set()
                for hit in results:
                    if not isinstance(hit, dict):
                        continue
                    doc = hit.get("document")
                    if not isinstance(doc, dict):
                        continue
                    key = doc.get("key")
                    if not key or key in seen_keys:
                        continue
                    seen_keys.add(key)
                    docs.append((doc, hit.get("score")))
                    if len(docs) >= limit:
                        break
        except Exception as exc:
            errors.append(f"{collection}-vector:{exc}")
        if not docs:
            try:
                payload = _mddb_post(
                    "search",
                    {"collection": collection, "limit": limit},
                    timeout=max(0.25, min(1.5, deadline - time.monotonic())),
                )
                if isinstance(payload, dict):
                    payload = payload.get("result", payload.get("docs", []))
                if isinstance(payload, list):
                    q_terms = _terms(query)
                    scored = []
                    for row in payload:
                        if not isinstance(row, dict):
                            continue
                        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
                        text = " ".join(
                            str(v or "")
                            for v in (
                                _ssot_meta_first(meta, "title"),
                                _ssot_meta_first(meta, "original_path"),
                                row.get("key"),
                            )
                        )
                        body = str(row.get("contentMd") or row.get("content") or "")[:4000]
                        score = len(q_terms & _terms(text)) * 3 + len(q_terms & _terms(body))
                        if score:
                            scored.append((score, row))
                    for _score, row in sorted(scored, key=lambda i: -i[0])[:limit]:
                        docs.append((row, _score))
            except Exception as exc:
                errors.append(f"{collection}:{exc}")
                continue
        for doc, score in docs[:limit]:
            content = str(doc.get("contentMd") or doc.get("content") or "")
            if not content and time.monotonic() < deadline - 0.3:
                try:
                    full = _mddb_post(
                        "get",
                        {
                            "collection": collection,
                            "key": doc.get("key"),
                            "lang": doc.get("lang") or "en",
                        },
                        timeout=max(0.25, min(1.0, deadline - time.monotonic())),
                    )
                    if isinstance(full, dict):
                        content = str(full.get("contentMd") or full.get("content") or "")
                except Exception:
                    pass
            hits.append(_ssot_hit(collection, doc, score, content, query))
    return hits, errors


SSOT_GET_COLLECTIONS = ("infrastructure-ssot", "chaba-glossary")


def _ssot_key_candidates(collection: str, key: str) -> list[str]:
    """Return MDDB key candidates for a user/agent supplied SSOT reference."""
    raw = (key or "").strip().strip("/")
    if not raw:
        return []
    candidates: list[str] = []

    def add(value: str) -> None:
        value = value.strip().strip("/")
        if value and value not in candidates:
            candidates.append(value)

    add(raw)
    if "/" not in raw and not raw.startswith(f"{collection}."):
        add(f"{collection}.{raw}")
    base = raw.rsplit("/", 1)[-1]
    if base.endswith((".yml", ".yaml")):
        base = base.rsplit(".", 1)[0]
    if base.startswith("ssot."):
        base = base[len("ssot."):]
    if base and base != raw:
        add(base)
        if "/" not in base and not base.startswith(f"{collection}."):
            add(f"{collection}.{base}")
    return candidates


def _ssot_get_doc(collection: str, key: str, timeout: float = 6.0) -> tuple[str, dict[str, Any]]:
    """Fetch one SSOT doc by key, tolerating bare/path-like key spellings."""
    candidates = _ssot_key_candidates(collection, key)
    if not candidates:
        raise LookupError("ssot key is required")
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            doc = _mddb_post(
                "get",
                {"collection": collection, "key": candidate, "lang": "en"},
                timeout=timeout,
            )
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            last_error = exc
            continue
        if isinstance(doc, dict) and (
            doc.get("key") or doc.get("contentMd") or doc.get("content")
        ):
            return candidate, doc
        last_error = RuntimeError("mddb get returned an empty document")
    raise LookupError(
        f"ssot doc not found in {collection}: {key}"
        + (f" ({last_error})" if last_error else "")
    )


def _ssot_timestamp(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value)


def _norm_selector_text(text: str) -> str:
    return re.sub(r"[-_\s]+", "-", (text or "").lower())


def _selector_variants(selector: str) -> set[str]:
    value = (selector or "").strip()
    for separator in ("=", ":"):
        if separator in value:
            value = value.rsplit(separator, 1)[-1]
    base = _norm_selector_text(value)
    if not base:
        return set()
    variants = {base}
    if base.startswith("tony-"):
        variants.add(base[len("tony-"):])
    else:
        variants.add(f"tony-{base}")
    return {variant for variant in variants if variant}


def _selector_block(content: str, selector: str, max_chars: int) -> dict[str, Any]:
    """Return a bounded block centered on a YAML/list selector when possible."""
    if not selector:
        return {
            "text": content[:max_chars],
            "matched": False,
            "line": None,
            "block_chars": len(content),
        }
    lines = content.split("\n")
    variants = _selector_variants(selector)
    norm_lines = [_norm_selector_text(line) for line in lines]
    best_score = 0
    best_index: int | None = None
    for index, norm_line in enumerate(norm_lines):
        stripped = norm_line.strip()
        score = 0
        for variant in variants:
            if variant not in norm_line:
                continue
            score += 1
            if stripped.startswith(f"{variant}:") or stripped.startswith(f"-{variant}:"):
                score += 3
        if score > best_score:
            best_score = score
            best_index = index
    if best_index is None or best_score == 0:
        return {
            "text": _excerpt_window(content, selector, max_chars),
            "matched": False,
            "line": None,
            "block_chars": len(content),
        }
    line = lines[best_index]
    indent = len(line) - len(line.lstrip())
    end = len(lines)
    for index in range(best_index + 1, len(lines)):
        candidate = lines[index]
        if not candidate.strip():
            continue
        candidate_indent = len(candidate) - len(candidate.lstrip())
        if candidate_indent <= indent:
            end = index
            break
    while end > best_index and not lines[end - 1].strip():
        end -= 1
    block = "\n".join(lines[best_index:end])
    text = block if len(block) <= max_chars else _excerpt_window(block, selector, max_chars)
    return {
        "text": text,
        "matched": True,
        "line": best_index + 1,
        "block_chars": len(block),
    }


def _related_pointers(content: str, exclude: tuple[str, ...] = (), limit: int = 8) -> list[str]:
    """Extract declared SSOT/doc pointers without treating them as truth."""
    if not content:
        return []
    excluded = {str(value).strip().lower() for value in exclude if value}
    patterns = (
        r"docs/ssot/[A-Za-z0-9_./-]+?\.yml",
        r"ssot\.[A-Za-z0-9][A-Za-z0-9._-]*?\.yml",
        r"(?:infrastructure|chaba|kanban|apps)-ssot\.[A-Za-z0-9][A-Za-z0-9._-]+",
    )
    found: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, content):
            value = match.group(0).rstrip(".,);`")
            lowered = value.lower()
            if lowered in excluded or lowered in seen:
                continue
            seen.add(lowered)
            found.append(value)
            if len(found) >= limit:
                return found
    return found


def _ssot_doc_provenance(doc: dict[str, Any]) -> dict[str, Any]:
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    freshness = (
        doc.get("updatedAt")
        or _ssot_meta_first(meta, "updated")
        or _ssot_meta_first(meta, "last_verified")
        or _ssot_meta_first(meta, "generated_at")
    )
    return {
        "key": doc.get("key"),
        "lang": doc.get("lang") or "en",
        "path": _ssot_meta_first(meta, "original_path") or "",
        "title": _ssot_meta_first(meta, "title"),
        "content_md5": _ssot_meta_first(meta, "content_md5"),
        "revision": doc.get("revision"),
        "added_at": _ssot_timestamp(doc.get("addedAt")),
        "updated_at": _ssot_timestamp(freshness),
        "freshness": _ssot_timestamp(freshness),
        "status": _ssot_meta_first(meta, "status") or "unknown",
    }


def _adobe_stock_index_root() -> Path:
    """Return the bounded workspace or read-only GDrive index directory."""
    default_gdrive = Path("/data/gdrive/ada-nest-adobe-stock/indexes")
    default_workspace = WORKSPACE / ".adobe-stock" / "indexes"
    configured = os.environ.get(
        "ADA_ADOBE_STOCK_INDEX_ROOT",
        str(default_gdrive if default_gdrive.is_dir() else default_workspace),
    )
    root = Path(configured).expanduser().resolve()
    workspace = WORKSPACE.resolve()
    approved_gdrive_root = Path(
        "/data/gdrive/ada-nest-adobe-stock/indexes"
    ).resolve()
    approved = (
        root == workspace
        or workspace in root.parents
        or root == approved_gdrive_root
        or approved_gdrive_root in root.parents
    )
    if not approved:
        raise ValueError(
            "Adobe Stock index root must be inside the workspace or "
            "the read-only ada-nest GDrive mount"
        )
    return root


def _read_adobe_stock_indexes() -> tuple[dict[str, list[Any]], list[str]]:
    """Read only the fixed JSON index files; never searches arbitrary paths."""
    root = _adobe_stock_index_root()
    indexes: dict[str, list[Any]] = {}
    missing: list[str] = []
    for filename in ADOBE_STOCK_INDEX_FILES:
        path = root / filename
        key = filename.removesuffix(".json")
        if not path.is_file():
            missing.append(str(path))
            indexes[key] = []
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload = payload.get("items", [])
        if not isinstance(payload, list):
            raise ValueError(f"{path} must contain a JSON list or an items list")
        indexes[key] = payload
    return indexes, missing


def _rank_adobe_items(items: list[Any], query: str, limit: int = 10) -> list[dict[str, Any]]:
    terms = _terms(query)
    ranked: list[tuple[int, dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(value) for value in item.values())
        score = len(terms & _terms(text))
        if score:
            ranked.append((score, item))
    return [
        {"score": score, **item}
        for score, item in sorted(ranked, key=lambda value: (-value[0], str(value[1].get("id", ""))))[:limit]
    ]


def _asset_preview(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize only explicitly exported, browser-safe preview fields."""
    preview_url = str(item.get("preview_url") or "").strip()
    thumbnail_url = str(item.get("thumbnail_url") or "").strip()
    valid_urls: dict[str, str | None] = {
        "preview_url": None,
        "thumbnail_url": None,
    }
    invalid_fields: list[str] = []
    for field, value in (("preview_url", preview_url), ("thumbnail_url", thumbnail_url)):
        if not value:
            continue
        try:
            parsed = urllib.parse.urlparse(value)
        except ValueError:
            parsed = None
        if (
            parsed
            and parsed.scheme in {"http", "https"}
            and parsed.netloc
            and not parsed.username
            and not parsed.password
        ):
            valid_urls[field] = value
        else:
            invalid_fields.append(field)

    warnings = [
        f"{field} is not a browser-safe http(s) URL"
        for field in invalid_fields
    ]
    if valid_urls["preview_url"] or valid_urls["thumbnail_url"]:
        status = "available" if valid_urls["preview_url"] else "thumbnail-only"
        reason = None
    elif invalid_fields:
        status = "unavailable"
        reason = "preview URL must be an http(s) URL without embedded credentials"
    else:
        status = "unavailable"
        reason = "no exported preview URL"

    return {
        "status": status,
        "preview_url": valid_urls["preview_url"],
        "thumbnail_url": valid_urls["thumbnail_url"],
        "source_ref": item.get("source_ref"),
        "reason": reason,
        "warnings": warnings,
    }


def _markdown_text(value: Any) -> str:
    """Keep exported asset labels safe when inserted into Markdown."""
    return str(value or "Untitled asset").replace("\\", "\\\\").replace(
        "[", "\\[").replace("]", "\\]")


def _preview_markdown(assets: list[dict[str, Any]]) -> str:
    """Build an OpenChamber-friendly preview block from normalized assets."""
    lines = ["### Adobe Stock previews", ""]
    if not assets:
        return "### Adobe Stock previews\n\nNo matching assets."
    for asset in assets:
        title = _markdown_text(asset.get("title") or asset.get("id"))
        preview = asset["preview"]
        lines.append(f"**{title}** (`{asset.get('id', 'unknown')}`)")
        if preview["thumbnail_url"]:
            lines.append(
                f"![{title}](<{preview['thumbnail_url']}>)"
            )
        if preview["preview_url"]:
            lines.append(
                f"[Open full preview](<{preview['preview_url']}>)"
            )
        if preview["status"] == "unavailable":
            lines.append(f"> Preview unavailable: {preview['reason']}.")
        elif preview["warnings"]:
            lines.append(f"> Preview warning: {'; '.join(preview['warnings'])}.")
        if preview["source_ref"]:
            lines.append(f"Source: `{preview['source_ref']}`")
        lines.append("")
    return "\n".join(lines).rstrip()


def _preview_html(assets: list[dict[str, Any]], campaign_idea: str) -> str:
    """Build a self-contained HTML preview page for OpenChamber Preview/browser."""
    import html as _html

    cards = []
    if not assets:
        cards.append('<p class="empty">No matching assets.</p>')
    for asset in assets:
        title = _html.escape(str(asset.get("title") or asset.get("id") or "untitled"))
        aid = _html.escape(str(asset.get("id") or "unknown"))
        preview = asset["preview"]
        thumb = preview.get("thumbnail_url") or preview.get("preview_url")
        img = (
            f'<a href="{_html.escape(preview["preview_url"] or preview["thumbnail_url"] or "#", quote=True)}" target="_blank" rel="noopener">'
            f'<img class="thumb" src="{_html.escape(thumb, quote=True)}" alt="{title}" loading="lazy"></a>'
            if thumb
            else '<div class="noimg">preview unavailable</div>'
        )
        links = ""
        if preview.get("preview_url"):
            links += (
                f'<a class="full" href="{_html.escape(preview["preview_url"], quote=True)}" '
                'target="_blank" rel="noopener">Open full preview</a>'
            )
        status = _html.escape(str(preview.get("status") or ""))
        warnings = "".join(
            f'<p class="warn">⚠ {_html.escape(w)}</p>'
            for w in preview.get("warnings") or []
        )
        if preview.get("status") == "unavailable":
            warnings += f'<p class="warn">Preview unavailable: {_html.escape(str(preview.get("reason") or ""))}</p>'
        source = _html.escape(str(preview.get("source_ref") or ""))
        tags = ", ".join(_html.escape(str(t)) for t in (asset.get("tags") or [])[:8])
        cards.append(
            f'<section class="card">{img}'
            f'<h3>{title}</h3><p class="meta"><code>{aid}</code> · {status}'
            f'{(" · " + tags) if tags else ""}</p>{links}{warnings}'
            f'<p class="src">Source: <code>{source}</code></p></section>'
        )
    idea = _html.escape(campaign_idea)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Adobe Stock previews — {idea}</title><style>"
        "body{font-family:system-ui,sans-serif;margin:24px;background:#0f1115;color:#e8e8e8}"
        "h1{font-size:20px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px}"
        ".card{background:#1a1d24;border:1px solid #2a2f3a;border-radius:10px;padding:12px}"
        ".thumb{width:100%;height:140px;object-fit:cover;border-radius:6px;background:#000;display:block}"
        ".noimg{height:140px;display:flex;align-items:center;justify-content:center;background:#000;border-radius:6px;color:#777}"
        "h3{font-size:14px;margin:10px 0 4px}.meta{font-size:12px;color:#9aa}.full{font-size:13px}"
        ".warn{color:#f0b429;font-size:12px}.src{font-size:11px;color:#667}.empty{color:#888}"
        "a{color:#7db4ff;text-decoration:none}code{background:#000;padding:1px 4px;border-radius:3px}"
        "</style></head><body>"
        f"<h1>Adobe Stock previews — {_html.escape(campaign_idea)}</h1>"
        f"<div class='grid'>{''.join(cards)}</div></body></html>"
    )


def _write_preview_html(html: str, campaign_idea: str) -> Path:
    """Persist the preview page under the bounded workspace previews dir."""
    import re as _re

    slug = _re.sub(r"[^a-z0-9]+", "-", campaign_idea.lower()).strip("-")[:48] or "preview"
    out_dir = WORKSPACE / ".adobe-stock" / "previews"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{slug}.html"
    path.write_text(html, encoding="utf-8")
    return path


def build_adobe_stock_reuse_report(
    campaign_idea: str,
    *,
    cards: list[dict[str, Any]] | None = None,
    memory: dict[str, Any] | None = None,
    indexes: dict[str, list[Any]] | None = None,
    missing_indexes: list[str] | None = None,
) -> dict[str, Any]:
    """Build a read-only, source-bound Adobe Stock reuse report."""
    idea = campaign_idea.strip()
    if not idea:
        raise ValueError("campaign_idea is required")
    indexes = indexes if indexes is not None else _read_adobe_stock_indexes()[0]
    if missing_indexes is None:
        _, missing_indexes = _read_adobe_stock_indexes()
    cards = cards if cards is not None else _board_cards()
    memory = memory if memory is not None else recall_memory(idea)
    related_cards = _rank_adobe_items(cards, idea, limit=10)
    lessons = memory.get("lessons", []) if isinstance(memory, dict) else []
    sessions = memory.get("devin_sessions", []) if isinstance(memory, dict) else []
    reusable_assets = _rank_adobe_items(indexes.get("assets", []), idea)
    for asset in reusable_assets:
        asset["preview"] = _asset_preview(asset)
    preview_statuses = [asset["preview"]["status"] for asset in reusable_assets]
    return {
        "artifact_type": "adobe_stock_reuse_report",
        "schema_version": 1,
        "status": "draft",
        "campaign_idea": idea,
        "related_cards": related_cards,
        "related_lessons": lessons[:10],
        "related_dev_sessions": sessions[:10],
        "related_campaigns": _rank_adobe_items(indexes.get("campaigns", []), idea),
        "reusable_assets": reusable_assets,
        "preview_summary": {
            "assets_returned": len(reusable_assets),
            "available": preview_statuses.count("available"),
            "thumbnail_only": preview_statuses.count("thumbnail-only"),
            "unavailable": preview_statuses.count("unavailable"),
        },
        "openchamber_preview_markdown": _preview_markdown(reusable_assets),
        "reusable_prompt_patterns": _rank_adobe_items(indexes.get("prompt-patterns", []), idea),
        "prior_rejection_patterns": _rank_adobe_items(indexes.get("rejection-patterns", []), idea),
        "facts": ["Every related item includes a source record from the bounded inputs."],
        "assumptions": [],
        "open_questions": [
            "Which generator and target batch size should this campaign use?",
            "Which related items should be reused?",
        ],
        "index_warnings": [f"missing index: {path}" for path in (missing_indexes or [])],
        "mutations_performed": [],
        "requires_native_followup": True,
        "next_menu": ["Create draft", "Revise", "Stop"],
    }


def verification_code(evidence: dict[str, Any]) -> str:
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=True).encode()).hexdigest()[:12].upper()
    return f"ADA-VFY-{digest}"


def _backlog_cards() -> list[dict[str, Any]]:
    fields = "id,title,brief,spec,column,priority,ref,updated,action,requests"
    query = urllib.parse.urlencode({
        "column": "backlog",
        "fields": fields,
        "limit": "1000",
    })
    payload = _get_json(f"/cards?{query}")
    if not isinstance(payload, dict) or not isinstance(payload.get("cards"), list):
        raise RuntimeError("board response did not contain projected cards")
    return [c for c in payload["cards"] if isinstance(c, dict)]


def _rank_next_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank safe, actionable backlog cards without mutating board state."""
    priority_points = {"high": 30, "medium": 20, "low": 10}
    safe_terms = ("test", "docs", "schema", "readonly", "read-only", "report", "opencode", "ada")
    ranked = []
    for card in cards:
        action = _card_action(card)
        status = str(action.get("status") or "").lower()
        if status in {"done", "running", "claimed", "idle", "held", "failed"}:
            continue
        if str(card.get("column") or "").lower() != "backlog":
            continue
        if not (card.get("brief") or card.get("spec")):
            continue
        card_id = str(card.get("id") or "")
        card_ref = card.get("ref")
        if not card_ref:
            continue
        title = str(card.get("title") or "")
        if title.lower().startswith("scenario fixture"):
            continue
        # Automated health/CMS cards need triage, not direct Ada/OpenCode
        # execution. Require a durable human-facing ref before recommending.
        if not card_ref and (
            card_id.startswith("cms-auto-") or card_id.startswith("audit-auto-")
        ):
            continue
        text = " ".join(str(card.get(k) or "") for k in ("title", "brief", "spec")).lower()
        score = priority_points.get(str(card.get("priority") or "").lower(), 0)
        reasons = []
        blockers = []
        if score:
            reasons.append(f"priority={card.get('priority')}")
        if len(str(card.get("brief") or card.get("spec") or "")) >= 80:
            score += 15
            reasons.append("self-contained brief")
        if any(term in text for term in ("test", "verify", "schema", "docs")):
            score += 15
            reasons.append("checkable artifact/test shape")
        if any(term in text for term in safe_terms):
            score += 5
            reasons.append("fits bounded Ada/OpenCode lane")
        if _positive_risk_terms(text):
            score -= 35
            blockers.append("risk keyword requires human/policy gate")
        if any(term in text for term in ("fixed", "completed", "closed", "already resolved")):
            score -= 50
            blockers.append("title suggests the work may already be resolved; verify before selecting")
        if card.get("requests"):
            score -= 25
            blockers.append("open decision/request")
        if str(action.get("type") or "").lower() == "manual":
            score -= 15
            blockers.append("manual action, not dispatchable")
        if not card.get("brief"):
            score -= 10
            blockers.append("thin brief")
        ranked.append({
            "id": card_id,
            "ref": card_ref,
            "selection_value": card_ref or card_id,
            "title": card.get("title"),
            "priority": card.get("priority"),
            "score": score,
            "reasons": reasons,
            "blockers": blockers,
            "recommended_command": f"/ada preflight {card_ref or card_id}",
        })
    ordered = sorted(ranked, key=lambda x: (-x["score"], str(x.get("ref") or x["id"])))
    safe = [candidate for candidate in ordered if not candidate["blockers"]]
    return (safe or ordered)[:5]


_CARD_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by",
    "can", "could", "did", "do", "does", "for", "from", "had", "has",
    "have", "how", "i", "if", "in", "into", "is", "it", "its", "me",
    "my", "no", "not", "of", "on", "or", "our", "please", "should",
    "the", "their", "them", "then", "there", "these", "they", "this",
    "to", "too", "up", "us", "was", "we", "were", "what", "when",
    "where", "which", "who", "why", "will", "with", "would", "you",
    "your", "about", "after", "again", "all", "also", "any", "because",
    "before", "being", "between", "both", "each", "few", "get", "got",
    "just", "like", "more", "most", "now", "off", "one", "only",
    "other", "out", "over", "own", "same", "some", "such", "than",
    "that", "card", "cards", "status", "project",
})


def _card_aliases(card: dict[str, Any]) -> set[str]:
    raw = card.get("aliases") or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(a).strip().lower() for a in raw if str(a).strip()}


def _card_text(card: dict[str, Any]) -> str:
    return " ".join(
        str(card.get(k) or "")
        for k in ("id", "ref", "title", "brief", "spec")
    ).lower()


def _reference_tokens(reference: str) -> list[str]:
    return [
        t for t in re.findall(r"[a-z0-9]+", reference.lower())
        if len(t) > 1 and t not in _CARD_STOPWORDS
    ]


def _ambiguity_error(reference: str, matches: list[dict[str, Any]]) -> LookupError:
    options = "; ".join(
        f"{c.get('ref') or c.get('id')} ({c.get('title')})"
        for c in matches[:5]
    )
    return LookupError(
        f"card reference {reference!r} matched {len(matches)} cards: {options}"
    )


def _hydrate_card(row: dict[str, Any]) -> dict[str, Any]:
    """Return the full single-card record; fall back to the index row."""
    full = _fetch_card_full(row.get("id"))
    return full if full is not None else row


def find_card(reference: str) -> dict[str, Any]:
    """Resolve a card by exact id/ref/alias, title substring, then scored
    natural-language token overlap. Ambiguous matches list candidates so the
    caller can retry with an exact ref.

    Resolution runs against the compact index first (id/ref only, ~0.6s),
    then the projected snapshot for aliases/title/token matching (~2s), and
    hydrates the winner with a single-card fetch — avoiding the 9MB/16s
    full /cards dump that used to blow the MCP client timeout.
    """
    needle = reference.strip().lower()
    if not needle:
        raise ValueError("card reference is required")

    def ids(row: dict[str, Any]) -> set[str]:
        return {str(row.get("id", "")).lower(), str(row.get("ref", "")).lower()}

    compact = _compact_cards()
    exact = [c for c in compact if needle in ids(c)]
    if len(exact) == 1:
        return _hydrate_card(exact[0])
    if len(exact) > 1:
        raise _ambiguity_error(reference, exact)

    cards = _board_cards()
    exact = [
        c for c in cards
        if needle in ids(c) or needle in _card_aliases(c)
    ]
    if len(exact) == 1:
        return _hydrate_card(exact[0])
    if len(exact) > 1:
        raise _ambiguity_error(reference, exact)

    titled = [
        c for c in cards
        if needle in str(c.get("title", "")).lower()
    ]
    if len(titled) == 1:
        return _hydrate_card(titled[0])
    if len(titled) > 1:
        raise _ambiguity_error(reference, titled)

    tokens = _reference_tokens(reference)
    if tokens:
        scored = []
        for c in cards:
            haystack = _card_text(c)
            hits = sum(
                1 for t in tokens
                if re.search(rf"\b{re.escape(t)}\b", haystack)
            )
            if hits:
                scored.append((hits, c))
        if scored:
            best = max(hits for hits, _ in scored)
            tops = [c for hits, c in scored if hits == best]
            if len(tops) == 1:
                return _hydrate_card(tops[0])
            raise _ambiguity_error(reference, tops)

    raise LookupError(f"no board card matched {reference!r}")


def _safe_card(card: dict[str, Any]) -> dict[str, Any]:
    """Keep receipts useful without returning arbitrary card internals."""
    return {
        "id": card.get("id"),
        "ref": card.get("ref"),
        "title": card.get("title"),
        "column": card.get("column"),
        "priority": card.get("priority"),
        "updated": card.get("updated"),
        "brief": card.get("brief"),
        "spec": card.get("spec"),
        "action": _card_action(card),
        "requests": card.get("requests") or [],
        "comms_tail": (card.get("comms") or [])[-5:],
        "comms_total": (
            card["comms_total"] if "comms_total" in card
            else len(card.get("comms") or [])
        ),
    }


def _context_card(reference: str, deadline: float) -> dict[str, Any]:
    """Resolve one card through board-api projected reads for context packs.

    Uses the compact board projection to map id/ref/title to a card id, then
    fetches only the fields `_safe_card` needs. This avoids the legacy full
    `/cards` dump inside an MCP request budget.
    """
    needle = reference.strip()
    if not needle:
        raise ValueError("card reference is required")
    lowered = needle.lower()
    fields = "id,ref,title,column,priority,updated,brief,spec,action,requests,comms"

    def remaining() -> float:
        return max(0.5, deadline - time.monotonic())

    def fetch_by_id(card_id: Any) -> dict[str, Any] | None:
        if card_id in (None, ""):
            return None
        payload = _get_json(
            f"/cards?id={urllib.parse.quote(str(card_id), safe='')}&limit=1"
            f"&fields={fields}&comms_tail={_HYDRATE_COMMS_TAIL}",
            timeout=remaining(),
        )
        rows = payload.get("cards") if isinstance(payload, dict) else []
        return rows[0] if rows and isinstance(rows[0], dict) else None

    exact_id = fetch_by_id(needle)
    if exact_id is not None:
        return exact_id

    compact = _get_json("/cards?compact=1&limit=1000", timeout=remaining())
    rows = compact.get("cards") if isinstance(compact, dict) else []
    rows = [row for row in rows if isinstance(row, dict)]
    exact = [
        row for row in rows
        if lowered in {str(row.get("id") or "").lower(), str(row.get("ref") or "").lower()}
    ]
    if len(exact) > 1:
        raise _ambiguity_error(reference, exact)
    titled = [row for row in rows if lowered in str(row.get("title") or "").lower()]
    if len(titled) > 1 and not exact:
        raise _ambiguity_error(reference, titled)
    candidate = exact[0] if exact else (titled[0] if titled else None)
    if candidate is None:
        raise LookupError(f"no board card matched {reference!r}")
    return fetch_by_id(candidate.get("id")) or candidate


def build_plan(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "operation": "plan",
        "card": _safe_card(card),
        "recommended_stages": [
            "read current board, repo, executor, and artifact contracts",
            "define canonical job-envelope schema and lifecycle transitions",
            "define result bundle and independent verifier contract",
            "write tests and examples without changing live services",
            "review with an independent executor before implementation",
        ],
        "allowed_scope": ["planning", "schema", "documentation", "tests", "examples"],
        "blocked_without_approval": ["live service changes", "credentials", "merge", "deploy", "self-modifying Ada runtime"],
        "next_safe_action": "produce a design artifact and acceptance checklist",
    }


def build_handoff_preview(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation": "handoff-preview",
        "card": {"id": card.get("id"), "ref": card.get("ref"), "title": card.get("title")},
        "envelope": {
            "card_id": card.get("id"),
            "task_class": "bounded development",
            "objective": card.get("brief"),
            "requirements": ["preserve board truth", "use an isolated worktree", "produce durable artifacts"],
            "executor": "devin-or-opencode-after-review",
            "allowed_paths": ["design docs", "schema", "tests", "examples"],
            "verify": ["schema validation", "unit tests", "independent review"],
            "approval_required": ["dispatch", "merge", "deploy", "self-modification"],
            "timeout_s": 900,
        },
        "mutations_performed": [],
        "next_safe_action": "review this preview, then explicitly dispatch through the board workflow",
    }


def _run(command: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(command, cwd=WORKSPACE, capture_output=True, text=True, timeout=10)
        return result.returncode, (result.stdout + result.stderr).strip()[:1200]
    except Exception as exc:
        return 1, str(exc)


def _plan(card_reference: str, include_memory: bool = False) -> dict[str, Any]:
    card = find_card(card_reference)
    plan = build_plan(card)
    if include_memory:
        plan["memory"] = recall_memory(
            " ".join(str(card.get(k) or "") for k in ("title", "brief", "spec"))
        )
    else:
        plan["memory"] = {
            "deferred": True,
            "next_tool": "ada_memory_search",
            "reason": "fast plan path; memory is opt-in",
        }
    plan["requires_native_followup"] = True
    plan["next_menu"] = ["Handoff preview", "Choose another card", "Stop"]
    return plan


@mcp.tool()
def ada_plan(card_reference: str, selection_confirmation: str = "", selection_token: str = "") -> str:
    """Read a card after exact native-menu confirmation; memory is deferred."""
    try:
        if selection_confirmation.strip().lower() != card_reference.strip().lower():
            return json.dumps({
                "ok": False,
                "operation": "plan",
                "error": "selection_confirmation_required",
                "expected": card_reference,
                "next_safe_action": "show a native confirmation menu and retry with the exact selected ref",
            }, ensure_ascii=False, indent=2)
        selection_error = _selection_gate(card_reference, selection_token)
        if selection_error:
            return json.dumps({
                "ok": False,
                "operation": "plan",
                "error": selection_error,
                "expected": card_reference,
                "next_safe_action": "use only a card and token from the current native candidate menu",
            }, ensure_ascii=False, indent=2)
        return json.dumps(_plan(card_reference), ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"ok": False, "operation": "plan", "error": str(exc), "board_api": BOARD_API})


@mcp.tool()
def ada_plan_with_context(card_reference: str, selection_confirmation: str = "", selection_token: str = "") -> str:
    """Read a confirmed card and explicitly include bounded context."""
    try:
        if selection_confirmation.strip().lower() != card_reference.strip().lower():
            return json.dumps({
                "ok": False,
                "operation": "plan-with-context",
                "error": "selection_confirmation_required",
                "expected": card_reference,
                "next_safe_action": "show a native confirmation menu and retry with the exact selected ref",
            }, ensure_ascii=False, indent=2)
        selection_error = _selection_gate(card_reference, selection_token)
        if selection_error:
            return json.dumps({
                "ok": False,
                "operation": "plan-with-context",
                "error": selection_error,
                "expected": card_reference,
                "next_safe_action": "use only a card and token from the current native candidate menu",
            }, ensure_ascii=False, indent=2)
        return json.dumps(_plan(card_reference, include_memory=True), ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"ok": False, "operation": "plan-with-context", "error": str(exc), "board_api": BOARD_API})


@mcp.tool()
def ada_memory_search(query: str) -> str:
    """Search Education lessons and local Devin summaries without mutating them."""
    try:
        return json.dumps({"ok": True, "operation": "memory-search", "memory": recall_memory(query)}, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"ok": False, "operation": "memory-search", "error": str(exc)}, ensure_ascii=False, indent=2)


@mcp.tool()
def ada_adobe_stock_reuse(campaign_idea: str, compact: bool = False) -> str:
    """Build a read-only Adobe Stock reuse report from bounded indexes and memory.

    compact=True returns only the preview summary and OpenChamber markdown —
    the full report exceeds tool-output display limits in OpenCode/OpenChamber.
    """
    # Misroute guard (ADA-1130): flash-lite models keep choosing this tool
    # for "adobe stock status"/QC questions. Bounce them to the right tool.
    _statusish = {"status", "qc", "campaign", "pending", "review",
                  "adobe stock status", "adobe stock qc"}
    if str(campaign_idea or "").strip().lower() in _statusish:
        return json.dumps({
            "ok": False, "operation": "adobe-stock-reuse",
            "error": "wrong_tool_for_status",
            "next_safe_action": "this is a campaign-status/QC question — "
                                "call ada_adobe_stock_qc instead and present "
                                "its candidate image + native menu"})
    try:
        indexes, missing = _read_adobe_stock_indexes()
        report = build_adobe_stock_reuse_report(
            campaign_idea,
            indexes=indexes,
            missing_indexes=missing,
        )
        if compact:
            preview_html_path = None
            try:
                preview_html_path = _write_preview_html(
                    _preview_html(
                        report.get("reusable_assets") or [],
                        str(report.get("campaign_idea") or campaign_idea),
                    ),
                    str(report.get("campaign_idea") or campaign_idea),
                )
            except Exception:
                preview_html_path = None
            return json.dumps(
                {
                    "ok": True,
                    "operation": "adobe-stock-reuse",
                    "compact": True,
                    "campaign_idea": report.get("campaign_idea"),
                    "preview_summary": report.get("preview_summary"),
                    "openchamber_preview_markdown": report.get(
                        "openchamber_preview_markdown"
                    ),
                    "preview_html_path": (
                        str(preview_html_path) if preview_html_path else None
                    ),
                    "mutations_performed": [],
                },
                ensure_ascii=False,
                indent=2,
            )
        return json.dumps(
            {"ok": True, "operation": "adobe-stock-reuse", "report": report},
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        return json.dumps({
            "ok": False,
            "operation": "adobe-stock-reuse",
            "error": str(exc),
            "mutations_performed": [],
        }, ensure_ascii=False, indent=2)


@mcp.tool()
def ada_self_verify() -> str:
    """Generate an evidence-bound verification code for the read-only bridge."""
    checks: dict[str, Any] = {
        "command_file": (WORKSPACE / ".opencode/commands/ada.md").exists(),
        "config_file": (WORKSPACE / ".opencode/opencode.json").exists(),
        "bridge_file": (WORKSPACE / ".opencode/ada_mcp.py").exists(),
    }
    try:
        card = find_card("ADA-1010")
        checks["card_id"] = card.get("id")
        checks["card_state"] = card.get("column")
        checks["board_reachable"] = True
    except Exception as exc:
        checks["board_reachable"] = False
        checks["board_error"] = str(exc)
    memory = recall_memory("ADA-1010 canonical Ada development control plane")
    checks["memory_lesson_hits"] = len(memory.get("lessons", []))
    checks["memory_devin_hits"] = len(memory.get("devin_sessions", []))
    checks["fixed_test_command"] = "python3 test_ada_mcp.py"
    checks["mutations_performed"] = []
    code = verification_code(checks)
    return json.dumps({
        "ok": all(v is True for k, v in checks.items() if k.endswith("_file") or k == "board_reachable"),
        "operation": "self-verify",
        "verification_code": code,
        "checks": checks,
        "independent_verification_required": True,
        "warning": "This code proves the read-only bridge checks completed; it is not approval of Ada's own work.",
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def ada_status(card_reference: str) -> str:
    """Read verified board state for a card or reference; never mutates it."""
    try:
        card = find_card(card_reference)
        # An explicit status target establishes active-card context and
        # invalidates any older candidate menu from this MCP process.
        PENDING_NEXT_SELECTIONS.clear()
        return json.dumps({"ok": True, "operation": "status", "card": _safe_card(card)}, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"ok": False, "operation": "status", "error": str(exc), "board_api": BOARD_API})


@mcp.tool()
def ada_next_card() -> str:
    """Rank the safest valuable backlog cards for Tony to review next."""
    try:
        cards = _backlog_cards()
        candidates = _rank_next_cards(cards)
        PENDING_NEXT_SELECTIONS.clear()
        for candidate in candidates:
            ref = str(candidate.get("ref") or candidate.get("id")).lower()
            token = secrets.token_urlsafe(12)
            candidate["selection_token"] = token
            PENDING_NEXT_SELECTIONS[ref] = (token, time.monotonic() + 900)
        return json.dumps({
            "ok": True,
            "operation": "next-card",
            "selection_policy": "priority + self-contained brief + checkable verifier - risk/open-decision/manual penalties",
            "candidates": candidates,
            "mutations_performed": [],
            "next_safe_action": "choose one candidate, confirm it, then call ada_preflight once",
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"ok": False, "operation": "next-card", "error": str(exc), "board_api": BOARD_API})


@mcp.tool()
def ada_handoff_preview(card_reference: str, plan_confirmation: str = "") -> str:
    """Build a handoff preview after exact plan-stage confirmation."""
    try:
        if plan_confirmation.strip().lower() != card_reference.strip().lower():
            return json.dumps({
                "ok": False,
                "operation": "handoff-preview",
                "error": "plan_confirmation_required",
                "expected": card_reference,
                "next_safe_action": "show the plan follow-up menu before retrying",
            }, ensure_ascii=False, indent=2)
        result = {"ok": True, **build_handoff_preview(find_card(card_reference))}
        result["requires_native_followup"] = True
        result["next_menu"] = ["Start supervised", "Revise", "Stop"]
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"ok": False, "operation": "handoff-preview", "error": str(exc), "board_api": BOARD_API})


_PICK_STRATEGIES = ("sweep", "high-value", "stale", "queue")
_CARD_PRIORITY_RANK = {"high": 3, "medium": 2, "low": 1}


def _card_updated_ts(card: dict[str, Any]) -> datetime:
    raw = str(card.get("updated") or card.get("created") or "")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:16], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def _card_actionable(card: dict[str, Any]) -> bool:
    """Not finished, not waiting on a human, not already owned."""
    if card.get("column") in ("done", "archive"):
        return False
    for r in card.get("requests") or []:
        if _request_open(r):
            return False
    if _card_action(card).get("status") in ("claimed", "running"):
        return False
    return True


def _pick_cards(strategy: str, days: int, limit: int) -> list[dict[str, Any]]:
    """Score actionable cards for one strategy; returns best-first rows."""
    now = datetime.now(timezone.utc)
    scored: list[tuple[dict[str, Any], str, tuple]] = []
    for card in _board_cards():
        if not _card_actionable(card):
            continue
        age = (now - _card_updated_ts(card)).days
        col = card.get("column") or "backlog"
        if strategy == "queue":
            if _card_action(card).get("status") == "queued":
                scored.append((card, "queued for dispatch", (0,)))
        elif strategy == "stale":
            if col == "doing" and age >= days:
                scored.append((card, f"doing, idle {age}d", (age,)))
        elif strategy == "sweep":
            scored.append((
                card,
                f"{col}, untouched {age}d",
                (1 if col == "doing" else 0, age),
            ))
        elif strategy == "high-value":
            rank = _CARD_PRIORITY_RANK.get(
                str(card.get("priority") or "").lower(), 0)
            if rank:
                scored.append((
                    card,
                    f"priority {card.get('priority')}, untouched {age}d",
                    (rank, age),
                ))
    if strategy in ("sweep", "high-value", "stale"):
        scored.sort(key=lambda t: t[2], reverse=True)
    return [{"card": c, "why": w} for c, w, _ in scored[:limit]]


@mcp.tool()
def ada_card_pick(strategy: str = "sweep", limit: int = 3, days: int = 7) -> str:
    """Pick actionable board cards by strategy; read-only, issues tokens.

    sweep      — stalest actionable card (doing first, then backlog)
    high-value — priority high>medium>low, ties by oldest updated
    stale      — doing cards idle >= `days` (default 7)
    queue      — cards with action.status == queued, board order

    Candidates carry a selection_token usable with ada_plan/ada_preflight.
    """
    try:
        if strategy not in _PICK_STRATEGIES:
            return json.dumps({
                "ok": False,
                "operation": "card-pick",
                "error": f"strategy must be one of {list(_PICK_STRATEGIES)}",
            }, ensure_ascii=False, indent=2)
        picks = _pick_cards(strategy, days, max(1, min(int(limit), 10)))
        PENDING_NEXT_SELECTIONS.clear()
        for pick in picks:
            card = pick["card"]
            ref = str(card.get("ref") or card.get("id") or "").lower()
            if ref:
                token = secrets.token_urlsafe(12)
                pick["selection_token"] = token
                PENDING_NEXT_SELECTIONS[ref] = (token, time.monotonic() + 900)
            pick["card"] = _safe_card(card)
        return json.dumps({
            "ok": True,
            "operation": "card-pick",
            "strategy": strategy,
            "picks": picks,
            "mutations_performed": [],
            "next_safe_action": (
                "confirm a pick, then call ada_card_context for the work "
                "pack or ada_preflight for the gated receipt"
            ),
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({
            "ok": False, "operation": "card-pick", "error": str(exc),
            "board_api": BOARD_API,
        }, ensure_ascii=False, indent=2)


_SWEEP_CARD_FIELDS = (
    "id,ref,title,column,updated,priority,action,action_status,requests"
)


def _sweep_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Bangkok"))


def _sweep_age_hours(card: dict[str, Any], now: datetime) -> float | None:
    raw = str(card.get("updated") or card.get("created") or "")
    for fmt, length in (
        ("%Y-%m-%d %H:%M", 16),
        ("%Y-%m-%dT%H:%M:%S", 19),
        ("%Y-%m-%d", 10),
    ):
        try:
            updated = datetime.strptime(raw[:length], fmt).replace(
                tzinfo=ZoneInfo("Asia/Bangkok"))
            return max(0.0, (now - updated).total_seconds() / 3600)
        except ValueError:
            continue
    return None


def _sweep_failure_class(result: str) -> tuple[str, str]:
    lower = result.lower()
    if "lane_envelope" in lower or "unstaged dispatch is held" in lower:
        return (
            "admission_blocked",
            "stage the declared lane envelope before retrying",
        )
    if "failed_precondition" in lower:
        return (
            "executor_precondition",
            "inspect the executor/account gate; do not blind-retry",
        )
    if "exit=" in lower or "failed with result" in lower:
        return (
            "runner_failed",
            "inspect the worktree/artifact before deciding whether to resume",
        )
    return (
        "failure_needs_review",
        "inspect the card and its linked artifacts before taking action",
    )


def _sweep_active_ids(queue: dict[str, Any]) -> set[str]:
    active_ids: set[str] = set()
    for row in queue.get("claimed", []):
        if isinstance(row, dict) and row.get("id"):
            active_ids.add(str(row["id"]))
    for host in (queue.get("hosts") or {}).values():
        if not isinstance(host, dict):
            continue
        for row in host.get("active", []):
            if isinstance(row, dict) and row.get("id"):
                active_ids.add(str(row["id"]))
    return active_ids


def _sweep_item(
    card: dict[str, Any],
    active_ids: set[str],
    window_hours: int,
    now: datetime,
) -> dict[str, Any] | None:
    action = _card_action(card)
    status = str(action.get("status") or card.get("action_status") or "").lower()
    age = _sweep_age_hours(card, now)
    is_active = str(card.get("id") or "") in active_ids
    request_rows = card.get("requests")
    if not isinstance(request_rows, list):
        request_rows = [request_rows] if request_rows else []
    open_requests = [
        request for request in request_rows
        if _request_open(request)
    ]
    category = ""
    next_step = ""
    failure_class = ""

    if status in ("running", "claimed"):
        if not is_active:
            category = "active_missing_from_queue"
            next_step = "reconcile the runner unit before changing the card"
        elif age is not None and age >= window_hours:
            category = "long_running_but_active"
            next_step = "verify live progress; do not interrupt an active runner"
        else:
            return None
    elif status == "failed":
        failure_class, next_step = _sweep_failure_class(
            str(action.get("result") or action.get("last_failure") or ""))
        category = f"failed_{failure_class}"
    elif status == "done" and card.get("column") == "doing":
        category = "done_still_in_doing"
        next_step = "verify the declared artifact, then move to review"
    elif status == "queued":
        if not is_active:
            category = "queued_missing_from_queue"
            next_step = "check queue eligibility and runner verdicts"
        else:
            return None
    elif card.get("column") == "doing" and age is not None and age >= window_hours:
        category = "stale_doing_without_active_action"
        next_step = "inspect current work before re-queueing or moving"
    elif open_requests:
        category = "awaiting_human"
        next_step = "resolve the existing card request before dispatch or closeout"
    else:
        return None

    if open_requests and status not in ("running", "claimed"):
        next_step += "; do not re-queue until answered"

    return {
        "id": str(card.get("id") or "")[:120],
        "ref": str(card.get("ref") or "")[:32],
        "title": str(card.get("title") or "")[:160],
        "column": str(card.get("column") or "")[:24],
        "action_status": status or "none",
        "action_type": str(action.get("type") or "")[:32],
        "updated": str(card.get("updated") or "")[:32],
        "age_hours": round(age, 1) if age is not None else None,
        "time_bucket": (
            "stale" if age is not None and age >= window_hours else "recent"
        ),
        "runner": str(action.get("runner") or "")[:80],
        "category": category,
        "failure_class": failure_class or None,
        "failure_noted_at": (
            action.get("sweep", {}).get("failed_noted")
            if isinstance(action.get("sweep"), dict) else None
        ),
        "open_request_count": len(open_requests),
        "active_in_queue": is_active,
        "next_safe_step": next_step,
    }


def _sweep_queue_items(queue: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for card in queue.get("queued", []):
        if not isinstance(card, dict):
            continue
        action = _card_action(card)
        blocked = card.get("blocked")
        blocked = blocked if isinstance(blocked, dict) else {}
        manual = (
            str(action.get("type") or "").lower() == "manual"
            or any("unknown type 'manual'" in str(v).lower()
                   for v in blocked.values())
        )
        if not manual and card.get("eligible"):
            continue
        items.append({
            "id": str(card.get("id") or "")[:120],
            "ref": str(card.get("ref") or "")[:32],
            "title": str(card.get("title") or "")[:160],
            "column": str(card.get("column") or "backlog")[:24],
            "action_status": "queued",
            "action_type": str(action.get("type") or "")[:32],
            "category": "manual_only" if manual else "queued_blocked",
            "blocked_hosts": list(blocked.keys())[:20],
            "next_safe_step": (
                "keep manual or define a supported executor; do not dispatch"
                if manual else
                "inspect runner verdicts; do not re-queue blindly"
            ),
        })
    return items


def _sweep_active_items(queue: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    host_active: dict[str, dict[str, Any]] = {}
    for host_name, host in (queue.get("hosts") or {}).items():
        if not isinstance(host, dict):
            continue
        for row in host.get("active", []):
            if isinstance(row, dict) and row.get("id"):
                active_row = dict(row)
                active_row.setdefault("host", host_name)
                host_active[str(row["id"])] = active_row

    for row in queue.get("claimed", []):
        if not isinstance(row, dict) or not row.get("id"):
            continue
        card_id = str(row["id"])
        host_row = host_active.pop(card_id, None)
        status = str(row.get("status") or "").lower()
        confirmed_active = bool(host_row) or (
            bool(row.get("unit")) and status in ("running", "claimed")
        )
        evidence = {**row, **(host_row or {})}
        items.append({
            "id": card_id[:120],
            "ref": str(evidence.get("ref") or "")[:32],
            "title": str(evidence.get("title") or "")[:160],
            "host": str(evidence.get("host") or "")[:80],
            "unit": str(evidence.get("unit") or "")[:160],
            "started_at": evidence.get("at"),
            "status": str(evidence.get("status") or row.get("status") or "claimed")[:24],
            "category": (
                "active_in_queue" if confirmed_active else "claim_without_active_unit"
            ),
            "next_safe_step": (
                "leave running; inspect only if progress stalls"
                if confirmed_active else
                "reconcile the claim/start state; no active unit is reported"
            ),
        })
    for card_id, row in host_active.items():
        items.append({
            "id": card_id[:120],
            "ref": str(row.get("ref") or "")[:32],
            "title": str(row.get("title") or "")[:160],
            "host": str(row.get("host") or "")[:80],
            "unit": str(row.get("unit") or "")[:160],
            "started_at": row.get("at"),
            "status": str(row.get("status") or "running")[:24],
            "category": "active_in_queue",
            "next_safe_step": "leave running; inspect only if progress stalls",
        })
    return items


@mcp.tool()
def ada_kanban_sweep(window_hours: int = 24, limit: int = 50) -> str:
    """Read-only, evidence-first snapshot of stuck Kanban work.

    Reads one queue snapshot and one projected `doing` slice. Failed and
    completed-but-stuck cards remain visible regardless of age; window_hours
    marks old active/idle work as stale. The tool never changes cards,
    retries jobs, contacts providers, or inspects host services.
    """
    try:
        hours = max(1, min(int(window_hours), 168))
        cap = max(1, min(int(limit), 100))
        queue = _get_json_cached("/queue")
        if (
            not isinstance(queue, dict)
            or not isinstance(queue.get("queued"), list)
            or not isinstance(queue.get("claimed"), list)
            or not isinstance(queue.get("hosts"), dict)
        ):
            raise ValueError("board-api /queue response has an invalid shape")

        path = (
            "/cards?column=doing&fields="
            f"{urllib.parse.quote(_SWEEP_CARD_FIELDS, safe=',')}"
            "&limit=1000"
        )
        payload = _get_json_cached(path)
        if not isinstance(payload, dict) or not isinstance(payload.get("cards"), list):
            raise ValueError("board-api /cards response has an invalid shape")

        now = _sweep_now()
        active_ids = _sweep_active_ids(queue)
        items = _sweep_queue_items(queue)
        for card in payload["cards"]:
            if isinstance(card, dict):
                item = _sweep_item(card, active_ids, hours, now)
                if item:
                    items.append(item)
        items.sort(key=lambda item: (
            0 if item["category"].startswith("failed_") else 1,
            -(item.get("age_hours") or 0),
        ))
        total = len(items)
        hosts = queue["hosts"]
        active_items = _sweep_active_items(queue)
        source_truncated = bool(payload.get("truncated"))
        category_counts: dict[str, int] = {}
        for item in items:
            category = item["category"]
            category_counts[category] = category_counts.get(category, 0) + 1
        return json.dumps({
            "ok": True,
            "operation": "kanban-sweep",
            "snapshot_at": now.isoformat(timespec="seconds"),
            "window_hours": hours,
            "queue": {
                "queued": len(queue["queued"]),
                "claimed": len(queue["claimed"]),
                "fresh_hosts": sum(
                    1 for h in hosts.values()
                    if isinstance(h, dict) and not h.get("stale", True)
                ),
                "stale_hosts": sum(
                    1 for h in hosts.values()
                    if isinstance(h, dict) and h.get("stale", True)
                ),
            },
            "counts": {
                "attention_items": total,
                "returned": min(total, cap),
                "truncated": total > cap or source_truncated,
                "by_category": category_counts,
            },
            "doing_cards_scanned": len(payload["cards"]),
            "doing_cards_total": payload.get("total", len(payload["cards"])),
            "source_truncated": source_truncated,
            "items": items[:cap],
            "active": active_items[:cap],
            "active_count": sum(
                1 for item in active_items if item["category"] == "active_in_queue"
            ),
            "active_truncated": len(active_items) > cap,
            "mutations_performed": [],
            "next_safe_action": (
                "inspect each item against its artifact; use board-api only "
                "after a human-approved decision"
            ),
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({
            "ok": False,
            "operation": "kanban-sweep",
            "error": str(exc)[:300],
            "board_api": BOARD_API,
            "mutations_performed": [],
        }, ensure_ascii=False, indent=2)


_CONTEXT_PATH_RE = re.compile(
    r"(?:^|[\s'\"(])((?:docs|scripts|integrations|stacks|apps|tests|"
    r"opencode|\.github|reports)/[\w./~:@+-]+)")
_CONTEXT_FILE_RE = re.compile(
    r"(?:^|[\s'\"(])([\w.+-]+\.(?:py|mjs|js|ts|sh|yml|yaml|md|json|php))")
_DONE_HINTS_RE = re.compile(
    r"(verif|done|deploy|expect|must|should|gate|check|test|confirm)",
    re.IGNORECASE)
_CONTEXT_MAX_COMMS = 5


def _context_card_text(card: dict[str, Any]) -> str:
    parts = [card.get("brief") or "", card.get("spec") or ""]
    for c in (card.get("comms") or [])[-_CONTEXT_MAX_COMMS:]:
        parts.append(str(c.get("text") if isinstance(c, dict) else c) or "")
    for r in card.get("requests") or []:
        if isinstance(r, dict):
            parts.append(str(r.get("ask") or ""))
            parts.append(str(r.get("answer") or ""))
        else:
            parts.append(str(r))
    return "\n".join(parts)


def _context_files(text: str, root: Path, max_files: int) -> list[Path]:
    """Path-shaped tokens that exist under the workspace, deduped."""
    seen, found = set(), []
    for m in _CONTEXT_PATH_RE.finditer(text):
        p = m.group(1).rstrip(".,;:)]")
        f = root / p
        if f.is_file() and p not in seen:
            seen.add(p)
            found.append(f)
    for m in _CONTEXT_FILE_RE.finditer(text):
        name = m.group(1)
        if "/" in name:
            continue
        for f in list(root.glob(f"**/{name}"))[:1]:
            rel = str(f.relative_to(root))
            if rel not in seen:
                seen.add(rel)
                found.append(f)
    return found[:max_files]


def _context_done_condition(card: dict[str, Any]) -> str:
    spec = (card.get("spec") or "").replace("\n", " ")
    hinted = [s.strip() for s in re.split(r"(?<=[.;:])\s+", spec)
              if _DONE_HINTS_RE.search(s)]
    if hinted:
        return hinted[-1]
    brief = (card.get("brief") or "").strip()
    return (brief[:200] if brief
            else "(no explicit done-condition — derive one from spec "
                 "before starting)")


def _context_pack(card: dict[str, Any], root: Path,
                  max_files: int = 8, head: int = 40) -> str:
    text = _context_card_text(card)
    files = _context_files(text, root, max_files)
    md = [f"# Card {card.get('ref') or card.get('id')}: {card.get('title')}", ""]
    md.append(f"- id: `{card.get('id')}`  ref: `{card.get('ref')}`")
    md.append(f"- column: `{card.get('column')}`  "
              f"priority: `{card.get('priority')}`")
    if card.get("brief"):
        md += ["", "## Brief", "", card["brief"]]
    if card.get("spec"):
        md += ["", "## Spec (verbatim)", "", card["spec"]]
    open_reqs = [r for r in (card.get("requests") or []) if _request_open(r)]
    if open_reqs:
        md += ["", "## Open requests (needs human input first)"]
        for r in open_reqs:
            if not isinstance(r, dict):
                md.append(f"- {r}")
                continue
            md.append(f"- {r.get('ask')} "
                      f"(options: {', '.join(r.get('options') or [])})")
    comms = [c for c in (card.get("comms") or [])[-_CONTEXT_MAX_COMMS:]]
    if comms:
        md += ["", "## Recent comms"]
        for c in comms:
            if isinstance(c, dict):
                body = str(c.get("text") or "")
                md.append(f"- `{c.get('at')}` {c.get('from')}: "
                          f"{body[:300]}{'…' if len(body) > 300 else ''}")
            else:
                md.append(f"- {str(c)[:300]}")
    for f in files:
        try:
            lines = f.read_text(errors="replace").splitlines()[:head]
        except OSError:
            continue
        md += ["", f"### {f.relative_to(root)}", "", "```",
               "\n".join(lines), "```"]
    md += ["", "## Gates & conventions", "",
           "- Work in a dispatch worktree; never edit the live checkout.",
           "- Card mutations go through board-api, never card-YAML edits.",
           "- Push via scripts/git-safe-push.sh; open a PR, let CI gate.",
           "- Verify real state before asserting done.",
           "- No secrets in chat/commits; never print token values.",
           "", "## Done-condition (synthesized — check before finishing)",
           "", _context_done_condition(card)]
    return "\n".join(md)


@mcp.tool()
def ada_card_context(card_reference: str, max_files: int = 8) -> str:
    """Build a compact work pack for one card; read-only.

    Card fields + open requests + recent comms + workspace files
    referenced by the card + gates + a synthesized done-condition.
    The pack is sized to start an opencode session without the whole board.
    """
    try:
        card = find_card(card_reference)
        pack = _context_pack(card, WORKSPACE, max(0, min(int(max_files), 12)))
        return json.dumps({
            "ok": True,
            "operation": "card-context",
            "card": _safe_card(card),
            "pack_markdown": pack,
            "mutations_performed": [],
            "next_safe_action": (
                "work the pack in this session, verify the done-condition, "
                "then hand the diff back for human review/commit"
            ),
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({
            "ok": False, "operation": "card-context", "error": str(exc),
            "board_api": BOARD_API,
        }, ensure_ascii=False, indent=2)



@mcp.tool()
def ada_context_pack(
    query: str,
    card_reference: str = "",
    profile: str = "ssot-research",
    include_memory: bool = True,
    include_ssot: bool = True,
    limit: int = 5,
) -> str:
    """Build a bounded read-only context pack for OpenChamber/OpenCode.

    Composes SSOT, Education, Devin-session memory, and an optional safe card
    snapshot into one provenance-labelled pack. It never reads personal or
    household banks and never mutates board/MDDB state.
    """
    try:
        profile_name = (profile or "ssot-research").strip() or "ssot-research"
        if profile_name not in {"ssot-research", "ops-readonly", "dev", "ada", "default"}:
            profile_name = "ssot-research"
        bounded_limit = max(1, min(int(limit), 5))
        deadline = time.monotonic() + 8.0
        errors: list[str] = []
        ssot: list[dict[str, Any]] = []
        memory: dict[str, Any] | None = None
        card: dict[str, Any] | None = None

        ref = (card_reference or "").strip()
        if ref:
            try:
                card = _safe_card(_context_card(ref, deadline))
            except Exception as exc:
                errors.append(f"card:{exc}")

        def _ssot_job() -> tuple[list[dict[str, Any]], list[str]]:
            if not include_ssot:
                return [], []
            return _ssot_search(query, deadline, limit=bounded_limit)

        def _memory_job() -> dict[str, Any] | None:
            if not include_memory:
                return None
            return recall_memory(query, deadline)

        futures: dict[str, concurrent.futures.Future] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            if include_ssot:
                futures["ssot"] = pool.submit(_ssot_job)
            if include_memory:
                futures["memory"] = pool.submit(_memory_job)
            done, _not_done = (
                concurrent.futures.wait(
                    futures.values(), timeout=max(0.1, deadline - time.monotonic())
                )
                if futures
                else (set(), set())
            )
            done_set = set(done)
            for name, future in futures.items():
                if future not in done_set:
                    errors.append(f"{name}:deadline")
                    continue
                try:
                    result = future.result()
                except Exception as exc:
                    errors.append(f"{name}:{exc}")
                    continue
                if name == "ssot":
                    ssot, ssot_errors = result
                    errors.extend(ssot_errors)
                elif result is not None:
                    memory = result
                    errors.extend(memory.get("errors") or [])

        sources = [
            "board-api:/cards",
            "mddb:chaba-lessons",
            "education:/lesson(fallback)",
            "mddb:ada-ha-bank-devin-tony",
            "mddb:infrastructure-ssot",
            "mddb:chaba-glossary",
        ]
        if card is not None:
            sources.append("board-api:/card")
        return json.dumps({
            "ok": True,
            "operation": "context-pack",
            "profile": profile_name,
            "query": query,
            "card_reference": ref or None,
            "allowed_sources": sources,
            "write_policy": "read-only; mutations require board-api approval flow",
            "budgets": {
                "max_results_per_source": bounded_limit,
                "excerpt_chars": 1200,
                "deadline_s": 8.0,
            },
            "ssot": ssot,
            "memory": memory,
            "card": card,
            "errors": errors,
            "provenance": {
                "board_api": BOARD_API,
                "mddb_cms": MDDB_CMS,
                "mddb_memory": MDDB_MEMORY,
            },
            "secrets_read": False,
            "mutations_performed": [],
            "next_safe_action": "use the excerpts as cited context; verify freshness before acting; do not mutate",
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({
            "ok": False,
            "operation": "context-pack",
            "error": str(exc),
            "board_api": BOARD_API,
            "mutations_performed": [],
        }, ensure_ascii=False, indent=2)

@mcp.tool()
def ada_ssot_get(collection: str, key: str, selector: str = "", max_chars: int = 12000) -> str:
    """Fetch one allowlisted SSOT document by collection/key after discovery.

    Use after ada_context_pack/vector search identifies the canonical SSOT key.
    Returns a bounded authoritative excerpt (optionally one YAML/list block via
    selector), provenance/freshness, related pointers, and mutations_performed=[].
    """
    operation = "ssot-get"
    try:
        coll = (collection or "").strip()
        doc_key = (key or "").strip()
        if coll not in SSOT_GET_COLLECTIONS:
            return json.dumps({
                "ok": False,
                "operation": operation,
                "error": f"collection not allowlisted: {coll}",
                "allowed_collections": list(SSOT_GET_COLLECTIONS),
                "mutations_performed": [],
            }, ensure_ascii=False, indent=2)
        if not doc_key:
            return json.dumps({
                "ok": False,
                "operation": operation,
                "error": "key is required",
                "mutations_performed": [],
            }, ensure_ascii=False, indent=2)
        try:
            bounded = int(max_chars)
        except (TypeError, ValueError):
            bounded = 12000
        bounded = max(1000, min(bounded, 40000))
        resolved_key, doc = _ssot_get_doc(coll, doc_key, timeout=6.0)
        content = str(doc.get("contentMd") or doc.get("content") or "")
        block = _selector_block(content, selector, bounded)
        provenance = _ssot_doc_provenance(doc)
        doc_path = str(provenance.get("path") or "")
        doc_base = doc_path.rsplit("/", 1)[-1] if doc_path else ""
        related = _related_pointers(
            content,
            exclude=(
                doc_path,
                f"docs/ssot/{doc_path}" if doc_path else "",
                doc_base,
                resolved_key,
                doc_key,
            ),
            limit=8,
        )
        excerpt = block["text"]
        return json.dumps({
            "ok": True,
            "operation": operation,
            "collection": coll,
            "requested_key": doc_key,
            "key": resolved_key,
            "selector": selector or None,
            "matched_selector": block["matched"],
            "selector_line": block["line"],
            "doc": provenance,
            "content": excerpt,
            "content_chars": len(content),
            "excerpt_chars": len(excerpt),
            "block_chars": block["block_chars"],
            "bounded": True,
            "related": related,
            "allowed_sources": [f"mddb:{coll}"],
            "write_policy": "read-only; mutations require board-api approval flow",
            "budgets": {"max_chars": bounded},
            "errors": [],
            "provenance": {
                "board_api": BOARD_API,
                "mddb_cms": MDDB_CMS,
            },
            "secrets_read": False,
            "mutations_performed": [],
            "next_safe_action": "cite doc.path/key/freshness; if the block is still not enough, narrow selector or ask for the specific field",
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({
            "ok": False,
            "operation": operation,
            "error": str(exc),
            "mutations_performed": [],
        }, ensure_ascii=False, indent=2)


SSOT_SEARCH_MAX_LIMIT = 20


def _ssot_hit_path(doc: dict[str, Any]) -> str:
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    path = str(_ssot_meta_first(meta, "original_path") or "").strip()
    if path and not path.startswith("docs/"):
        path = f"docs/ssot/{path}"
    return path


@mcp.tool()
def ada_ssot_search(query: str, collection: str = "", limit: int = 10) -> str:
    """Bounded lexical search over allowlisted SSOT documents (read-only).

    Comparable to the ssot-search skill / mcp_search_ssot: exact term and
    path matching over SSOT YAML mirrored in MDDB, via the lexical /v1/fts
    endpoint — no embedding or provider calls, so it costs nothing. Use it
    for precise lookups; ada_context_pack remains the semantic-discovery
    surface and ada_ssot_get hydrates one hit by key. Returns matched SSOT
    paths, excerpts, matched terms, provenance/freshness, and
    cross-reference pointers with mutations_performed=[].
    """
    operation = "ssot-search"
    try:
        q = (query or "").strip()
        if not q:
            return json.dumps({
                "ok": False,
                "operation": operation,
                "error": "query is required",
                "mutations_performed": [],
            }, ensure_ascii=False, indent=2)
        try:
            cap = int(limit)
        except (TypeError, ValueError):
            cap = 10
        cap = max(1, min(cap, SSOT_SEARCH_MAX_LIMIT))
        requested = (collection or "").strip()
        if requested:
            if requested not in SSOT_GET_COLLECTIONS:
                return json.dumps({
                    "ok": False,
                    "operation": operation,
                    "error": f"collection not allowlisted: {requested}",
                    "allowed_collections": list(SSOT_GET_COLLECTIONS),
                    "mutations_performed": [],
                }, ensure_ascii=False, indent=2)
            collections = (requested,)
        else:
            collections = SSOT_GET_COLLECTIONS

        hits: list[dict[str, Any]] = []
        errors: list[str] = []
        totals: dict[str, int] = {}
        deadline = time.monotonic() + 8.0
        for coll in collections:
            remaining = max(0.5, min(4.0, deadline - time.monotonic()))
            if remaining <= 0.5:
                errors.append(f"{coll}:deadline")
                continue
            try:
                payload = _mddb_post(
                    "fts",
                    {"collection": coll, "query": q, "limit": cap},
                    timeout=remaining,
                )
            except Exception as exc:
                errors.append(f"{coll}-fts:{exc}")
                continue
            if not isinstance(payload, dict):
                errors.append(f"{coll}-fts:invalid response")
                continue
            if isinstance(payload.get("total"), int):
                totals[coll] = payload["total"]
            for row in payload.get("results") or []:
                if not isinstance(row, dict):
                    continue
                doc = row.get("document")
                if not isinstance(doc, dict):
                    continue
                content = str(doc.get("contentMd") or doc.get("content") or "")
                provenance = _ssot_doc_provenance(doc)
                path = _ssot_hit_path(doc)
                hits.append({
                    "collection": coll,
                    "key": doc.get("key"),
                    "path": path,
                    "title": provenance.get("title"),
                    "score": row.get("score"),
                    "matched_terms": [
                        str(t)[:40] for t in (row.get("matchedTerms") or [])[:12]
                    ],
                    "excerpt": _excerpt_window(content, q, 1200),
                    "excerpt_chars": min(len(content), 1200),
                    "doc": provenance,
                    "related": _related_pointers(
                        content,
                        exclude=(path, doc.get("key") or ""),
                        limit=5,
                    ),
                })
        hits.sort(key=lambda h: -(h.get("score") or 0))
        return json.dumps({
            "ok": True,
            "operation": operation,
            "query": q,
            "collections": list(collections),
            "source": "mddb-fts",
            "match_mode": "lexical",
            "counts": {
                "returned": min(len(hits), cap),
                "total_hits": len(hits),
                "per_collection_total": totals,
                "truncated": len(hits) > cap,
            },
            "results": hits[:cap],
            "errors": errors,
            "allowed_sources": [f"mddb:{c}" for c in collections],
            "write_policy": "read-only; mutations require board-api approval flow",
            "budgets": {"max_results": cap, "excerpt_chars": 1200, "deadline_s": 8.0},
            "provenance": {
                "board_api": BOARD_API,
                "mddb_cms": MDDB_CMS,
                "mddb_memory": MDDB_MEMORY,
            },
            "secrets_read": False,
            "mutations_performed": [],
            "next_safe_action": (
                "hydrate a hit with ada_ssot_get(collection, key); "
                "use ada_context_pack for semantic discovery"
            ),
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({
            "ok": False,
            "operation": operation,
            "error": str(exc)[:300],
            "mutations_performed": [],
        }, ensure_ascii=False, indent=2)


READ_ONLY_PROFILES = {"ops-readonly", "ssot-research", "mddb-sequence", "storage-dryrun"}


def _resolved_verifier(action: dict[str, Any]) -> str | None:
    """Resolve a verifier from an explicit card field or a trusted profile."""
    explicit = action.get("verify") or action.get("verifier")
    if explicit:
        return str(explicit)
    profile = str(action.get("agent_profile") or action.get("profile") or "").strip()
    if action.get("executor") == "opencode" and profile in READ_ONLY_PROFILES:
        return f"profile:{profile}"
    return None


@mcp.tool()
def ada_preflight(
    card_reference: str,
    selection_confirmation: str = "",
    selection_token: str = "",
) -> str:
    """Run all read-only gates once and return one workflow receipt."""
    try:
        if selection_confirmation.strip().lower() != card_reference.strip().lower():
            return json.dumps({
                "ok": False,
                "operation": "preflight",
                "error": "selection_confirmation_required",
                "expected": card_reference,
            }, ensure_ascii=False, indent=2)
        selection_error = _selection_gate(card_reference, selection_token)
        if selection_error:
            return json.dumps({
                "ok": False,
                "operation": "preflight",
                "error": selection_error,
                "expected": card_reference,
                "next_safe_action": "use only a card and token from the current native candidate menu",
            }, ensure_ascii=False, indent=2)

        card = find_card(card_reference)
        action = _card_action(card)
        text = " ".join(str(card.get(k) or "") for k in ("title", "brief", "spec")).lower()
        risky = _positive_risk_terms(text)
        start_gates = {
            "card_found": True,
            "backlog": card.get("column") == "backlog",
            "no_open_requests": not bool(card.get("requests")),
            "safe_scope": not bool(risky),
            "typed_dispatch_available": False,
        }
        required = ("type", "repo", "executor", "model", "profile", "target_host", "artifact")
        aliases = {"profile": ("profile", "agent_profile"), "artifact": ("artifact", "output_artifact")}
        missing = [
            field for field in required
            if not any(action.get(name) for name in aliases.get(field, (field,)))
        ]
        verifier = _resolved_verifier(action)
        if not verifier:
            missing.append("verifier")
        dispatch = {
            "ok": not missing,
            "required_fields": list(required) + ["verifier"],
            "missing_fields": sorted(set(missing)),
            "resolved_verifier": verifier,
            "typed_dispatch_available": False,
            "approval_required": True,
        }
        workflow_id = f"ada-preflight-{secrets.token_urlsafe(12)}"
        ready = all(start_gates.values()) and dispatch["ok"]
        return json.dumps({
            "ok": True,
            "operation": "preflight",
            "workflow_id": workflow_id,
            "card": _safe_card(card),
            "plan": _plan(card_reference),
            "handoff": build_handoff_preview(card),
            "start_check": {
                "ok": all(start_gates.values()),
                "gates": start_gates,
                "risk_terms": risky,
                "executor": action.get("executor") or "not declared",
            },
            "dispatch_check": dispatch,
            "ready_for_dispatch": ready,
            "mutations_performed": [],
            "requires_native_followup": True,
            "next_menu": (
                ["Approve dispatch", "Revise", "Stop"]
                if ready else ["Review preflight", "Revise", "Stop"]
            ),
            "next_safe_action": (
                "typed dispatch is not wired; review the receipt and stop"
                if not ready else "require explicit approval before dispatch"
            ),
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"ok": False, "operation": "preflight", "error": str(exc), "board_api": BOARD_API})


@mcp.tool()
def ada_start_check(card_reference: str, handoff_confirmation: str = "") -> str:
    """Validate start after exact handoff-stage confirmation."""
    try:
        if handoff_confirmation.strip().lower() != card_reference.strip().lower():
            return json.dumps({
                "ok": False,
                "operation": "start-check",
                "error": "handoff_confirmation_required",
                "expected": card_reference,
                "next_safe_action": "show the handoff decision menu before retrying",
            }, ensure_ascii=False, indent=2)
        card = find_card(card_reference)
        action = _card_action(card)
        text = " ".join(str(card.get(k) or "") for k in ("title", "brief", "spec")).lower()
        risky = _positive_risk_terms(text)
        gates = {
            "card_found": True,
            "backlog": card.get("column") == "backlog",
            "no_open_requests": not bool(card.get("requests")),
            "safe_scope": not bool(risky),
            "typed_dispatch_available": False,
        }
        return json.dumps({
            "ok": all(gates.values()) and not risky,
            "operation": "start-check",
            "card": _safe_card(card),
            "gates": gates,
            "risk_terms": risky,
            "executor": action.get("executor") or "not declared",
            "approval_required": True,
            "mutations_performed": [],
            "next_safe_action": (
                "typed dispatch is not wired; keep the card review-ready and "
                "do not dispatch from this tool"
            ),
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"ok": False, "operation": "start-check", "error": str(exc), "board_api": BOARD_API})


_DISPATCH_REQUIRED = ("type", "repo", "executor", "model", "profile",
                      "target_host", "artifact")
_DISPATCH_ALIASES = {"profile": ("profile", "agent_profile"),
                     "artifact": ("artifact", "output_artifact")}


def _dispatch_missing(action: dict[str, Any]) -> list[str]:
    """Fields ada_dispatch_check requires — shared by draft/stage."""
    missing = [f for f in _DISPATCH_REQUIRED
               if not any(action.get(n)
                          for n in _DISPATCH_ALIASES.get(f, (f,)))]
    if not _resolved_verifier(action):
        missing.append("verifier")
    return sorted(set(missing))


def _card_action_state(card: dict[str, Any]) -> tuple[str, str]:
    """(action.status, runner) — runner '' when the card is unclaimed."""
    a = _card_action(card)
    claim = card.get("claim") if isinstance(card.get("claim"), dict) else {}
    runner = str(a.get("runner") or claim.get("runner")
                 or claim.get("host") or "")
    return str(a.get("status") or "idle"), runner


@mcp.tool()
def ada_dispatch_check(card_reference: str) -> str:
    """Validate the typed dispatch envelope without creating a job."""
    try:
        card = find_card(card_reference)
        action = _card_action(card)
        missing = _dispatch_missing(action)
        state, runner = _card_action_state(card)
        live = state in ("queued", "running")
        return json.dumps({
            "ok": not missing,
            "operation": "dispatch-check",
            "card": {"id": card.get("id"), "ref": card.get("ref"), "title": card.get("title")},
            "required_fields": list(_DISPATCH_REQUIRED) + ["verifier"],
            "missing_fields": missing,
            "action_status": state,
            "runner": runner or None,
            "blocked": live,
            "blocked_reason": ("card is claimed/running — do not queue "
                               "or re-stage until the attempt ends"
                               if live else None),
            "typed_dispatch_available": False,
            "approval_required": True,
            "mutations_performed": [],
            "envelope_tool": ("ada_envelope_draft" if missing else None),
            "next_safe_action": (
                "call ada_envelope_draft for the missing fields, review "
                "the payload, then ada_envelope_stage with confirmed=true "
                "after explicit approval; this check never mutates"
            ),
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"ok": False, "operation": "dispatch-check", "error": str(exc), "board_api": BOARD_API})


# --- staged OpenCode dispatch envelopes --------------------------------
# Mirror of the runner's S3 staged-dispatch admission
# (runner-agent.S3_STAGED_PROFILES + prepare_profile_dispatch): only
# these profiles on stage S3 / target idc02 can be admitted today, so
# the draft builder offers exactly them. Values mirror
# evals/opencode-lane/profiles/<id>/profile.yml on chaba master and the
# live envelopes on ada-ocb-free-lane-workflow-pilot /
# ada-docs-search-drift. The runner re-validates the envelope at claim
# (lint_envelope + check_envelope), so drift fails closed — held, never
# misrouted.
_STAGED_TARGET_HOST = "idc02"
_LANE_PROFILE_DEFAULT = {"free_lane": "ops-readonly"}
# action keys board-api's /card write door accepts (mirror of
# board-api.ACTION_KEYS) — status/runner/task_id/attempts stay
# board-owned and are never caller-settable.
_ENVELOPE_ACTION_KEYS = (
    "type", "repo", "host", "labels", "button", "model", "executor",
    "agent_profile", "profile", "target_host", "artifact", "task_class",
    "prompt", "expect", "lane", "subject_host", "llm_key_alias",
    "lane_envelope", "nlm_notebook", "nlm_source_file",
    "nlm_source_text", "nlm_source_url")

_STAGED_PROFILES: dict[str, dict[str, Any]] = {
    "ops-readonly": {
        "task_classes": ("read_only", "report"),
        "providers": ("litellm", "opencode"),
        "models": (
            "litellm/gemini-3.5-flash-lite", "litellm/gemini-3.5-flash",
            "litellm/or-free-nemotron-nano", "litellm/or-free-nemotron-ultra",
            "litellm/or-free-nemotron-35", "litellm/or-free-north-code",
            "litellm/or-gemini-3.5-flash-lite", "litellm/or-gemini-3.5-flash",
            "opencode/big-pickle", "opencode/deepseek-v4.1-flash-free",
            "opencode/glm-5.3-flash-free", "opencode/mimo-v2.6-flash-free"),
        "artifact_dirs": ("evals/opencode-lane/out/", "reports/"),
        "artifact_schema": ("evals/opencode-lane/profiles/ops-readonly/"
                            "fixture/task.yaml #contract.required_fields"),
        "verifier": {"type": "held_out_py",
                     "ref": "evals/opencode-lane/profiles/ops-readonly/"
                            "fixture/verify.py"},
        "max_retries": 1,
        "default_model": "litellm/gemini-3.5-flash-lite",
    },
    "ssot-research": {
        "task_classes": ("read_only", "report"),
        "providers": ("litellm", "opencode"),
        "models": (
            "litellm/gemini-3.5-flash-lite", "litellm/gemini-3.5-flash",
            "litellm/ollama-qwen38b-kkmac", "litellm/ollama-qwen32b-kkmac",
            "litellm/or-free-nemotron-ultra", "litellm/or-free-nemotron-35",
            "litellm/or-free-north-code",
            "opencode/big-pickle", "opencode/deepseek-v4.1-flash-free",
            "opencode/glm-5.3-flash-free", "opencode/mimo-v2.6-flash-free"),
        "artifact_dirs": ("evals/opencode-lane/out/", "reports/"),
        "artifact_schema": ("evals/opencode-lane/profiles/ssot-research/"
                            "fixture/task.yaml #contract.required_fields"),
        "verifier": {"type": "held_out_py",
                     "ref": "evals/opencode-lane/profiles/ssot-research/"
                            "fixture/verify.py"},
        "max_retries": 1,
        "default_model": "litellm/gemini-3.5-flash-lite",
    },
    "storage-dryrun": {
        "task_classes": ("read_only", "report"),
        "providers": ("litellm",),
        "models": ("litellm/gemini-3.5-flash-lite",
                   "litellm/gemini-3.5-flash"),
        "artifact_dirs": ("evals/opencode-lane/out/", "reports/"),
        "artifact_schema": ("evals/opencode-lane/profiles/storage-dryrun/"
                            "fixture/task.yaml #contract.required_fields"),
        "verifier": {"type": "held_out_py",
                     "ref": "evals/opencode-lane/profiles/storage-dryrun/"
                            "fixture/verify.py"},
        "max_retries": 0,
        "default_model": "litellm/gemini-3.5-flash-lite",
    },
}


def _artifact_dir_for(art: str, dirs: tuple) -> str | None:
    for d in dirs:
        if art.startswith(d):
            return d
    return None


def _build_staged_action(card: dict[str, Any],
                         provided: dict[str, Any],
                         ) -> tuple[dict[str, Any], list[str], list[str]]:
    """Merge caller fields over the card's existing action and grow a
    complete lane_envelope for the admitted staged profiles.
    Returns (payload, problems, notes). payload carries board-api
    ACTION_KEYS only — lifecycle fields stay board-owned."""
    cid = str(card.get("id") or "")
    merged = {k: v for k, v in _card_action(card).items()
              if k in _ENVELOPE_ACTION_KEYS}
    problems: list[str] = []
    notes: list[str] = []
    unknown = sorted(set(provided) - set(_ENVELOPE_ACTION_KEYS))
    for k in unknown:
        notes.append(f"ignored non-action field {k!r}")
    for k, v in provided.items():
        if k in _ENVELOPE_ACTION_KEYS:
            merged[k] = v
    if "lane_envelope" in provided:
        notes.append("caller-supplied lane_envelope replaced by the "
                     "generated staged envelope")

    executor = str(merged.get("executor") or "").strip()
    lane = str(merged.get("lane") or "").strip()
    profile = str(merged.get("agent_profile")
                  or merged.get("profile") or "").strip()
    if not executor:
        if profile in _STAGED_PROFILES or lane in _LANE_PROFILE_DEFAULT:
            executor = "opencode"
            merged["executor"] = executor
            notes.append("executor defaulted to opencode for the staged "
                         "profile lane")
        else:
            problems.append(
                "action.executor required — staged OpenCode dispatch "
                "needs executor=opencode plus a resolvable profile")
    if executor == "opencode":
        if not profile:
            profile = _LANE_PROFILE_DEFAULT.get(lane, "")
            if profile:
                merged["agent_profile"] = profile
                notes.append(f"profile resolved from lane {lane!r}: "
                             f"{profile}")
        merged["agent_profile"] = profile or merged.get("agent_profile")
        spec = _STAGED_PROFILES.get(profile)
        if spec is None:
            problems.append(
                f"profile {profile or '(none)'} is not admitted for "
                f"staged dispatch — admitted: "
                f"{sorted(_STAGED_PROFILES)}")
        else:
            merged.setdefault("type", "dispatch")
            model = str(merged.get("model")
                        or spec["default_model"]).strip()
            provider = (model.split("/", 1)[0] if "/" in model
                        else "devin")
            if model not in spec["models"]:
                problems.append(
                    f"model {model!r} is not allowed for profile "
                    f"{profile}")
            if provider not in spec["providers"]:
                problems.append(
                    f"provider {provider!r} is not allowed for profile "
                    f"{profile}")
            merged["model"] = model
            th = str(merged.get("target_host") or "").strip()
            if not th:
                th = _STAGED_TARGET_HOST
                merged["target_host"] = th
                notes.append(f"target_host defaulted to {_STAGED_TARGET_HOST} "
                             "(the only host admitted for staged S3 "
                             "dispatch today)")
            elif th != _STAGED_TARGET_HOST:
                problems.append(
                    f"target_host {th!r} is not admitted — staged S3 "
                    f"dispatch currently requires {_STAGED_TARGET_HOST}")
            art = str(merged.get("artifact")
                      or merged.get("output_artifact") or "").strip()
            if not art:
                problems.append(
                    "action.artifact required — a repo-relative path "
                    "under " + "/".join(spec["artifact_dirs"]))
            elif (art.startswith("/") or ".." in art.split("/")
                  or _artifact_dir_for(art, spec["artifact_dirs"])
                  is None):
                problems.append(
                    f"artifact {art!r} must be a safe repo-relative "
                    "path under " + "/".join(spec["artifact_dirs"]))
            tc = str(merged.get("task_class") or "read_only").strip()
            if tc not in ("read_only", "report"):
                problems.append(
                    "opencode task_class must be read_only or report")
            elif tc not in spec["task_classes"]:
                problems.append(
                    f"task_class {tc!r} not admitted for profile "
                    f"{profile}")
            merged["task_class"] = tc
            if not merged.get("repo"):
                problems.append("action.repo required (dispatch "
                                "whitelist: chaba, ada-voice, ...)")
            art_dir = (_artifact_dir_for(art, spec["artifact_dirs"])
                       or spec["artifact_dirs"][0])
            merged["lane_envelope"] = {
                "id": f"s3-{cid}-{profile}"[:80],
                "card": cid,
                "task_class": tc,
                "stage": "S3",
                "target_host": _STAGED_TARGET_HOST,
                "executor": "opencode",
                "profile": profile,
                "model": model,
                "provider": provider,
                "unattended": True,
                "mcp_allowlist": [],
                "allowed_paths": [art_dir],
                "artifact": {"path": art,
                             "schema": spec["artifact_schema"]},
                "verifier": dict(spec["verifier"]),
                "escalation": {
                    "on_fail": "manual",
                    "allowed_on_fail": ["manual", "devin",
                                        "retry_then_manual", "hold"],
                    "max_retries": spec["max_retries"],
                },
                "rollback": {"kind": "none"},
                "heartbeat": {"required": True, "stale_s": 300},
                "timeout_s": 420,
            }
    elif executor:
        notes.append(f"executor {executor!r} does not use "
                     "lane_envelope — staged envelope not generated")
    return ({k: v for k, v in merged.items()
             if k in _ENVELOPE_ACTION_KEYS}, problems, notes)


@mcp.tool()
def ada_envelope_draft(card_reference: str,
                       action: dict[str, Any] | None = None) -> str:
    """Build a complete dispatch action (including action.lane_envelope)
    for a card — read-only, writes nothing. `action` carries the
    caller-owned fields to set (repo, executor, agent_profile/profile,
    target_host, artifact, model, task_class, lane, prompt, expect,
    llm_key_alias); missing pieces are defaulted from the admitted
    staged profiles. Review the returned payload, then pass it to
    ada_envelope_stage."""
    try:
        card = find_card(card_reference)
    except Exception as exc:
        return json.dumps({"ok": False, "operation": "envelope-draft",
                           "error": str(exc),
                           "mutations_performed": []})
    payload, problems, notes = _build_staged_action(card, action or {})
    missing = _dispatch_missing(payload)
    state, runner = _card_action_state(card)
    ok = not problems and not missing
    return json.dumps({
        "ok": ok,
        "operation": "envelope-draft",
        "card": {"id": card.get("id"), "ref": card.get("ref"),
                 "title": card.get("title")},
        "action_status": state,
        "runner": runner or None,
        "action": payload,
        "missing_fields": missing,
        "problems": problems,
        "notes": notes,
        "admitted_profiles": sorted(_STAGED_PROFILES),
        "staged_target_host": _STAGED_TARGET_HOST,
        "next_safe_action": (
            "review the action payload; on approval call "
            "ada_envelope_stage with the same action and "
            "confirmed=true — it writes the envelope AND queues the "
            "card in one board-api call" if ok else
            "fix missing_fields/problems and re-draft; nothing was "
            "written"),
        "mutations_performed": [],
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def ada_envelope_stage(card_reference: str,
                       action: dict[str, Any] | None = None,
                       confirmed: bool = False) -> str:
    """Stage a complete dispatch envelope on a card AND queue it —
    the only Ada path that writes action.lane_envelope (via board-api
    /card on_exists=queue). Two-call confirmation gate; refuses while
    the card is queued or running."""
    try:
        card = find_card(card_reference)
    except Exception as exc:
        return json.dumps({"ok": False, "operation": "envelope-stage",
                           "error": str(exc),
                           "mutations_performed": []})
    state, runner = _card_action_state(card)
    if state in ("queued", "running"):
        return json.dumps({
            "ok": False,
            "operation": "envelope-stage",
            "card": {"id": card.get("id"), "ref": card.get("ref"),
                     "title": card.get("title")},
            "action_status": state,
            "runner": runner or None,
            "error": f"card is {state}"
                     + (f" on {runner}" if runner else "")
                     + " — staging would fight a live attempt",
            "blocked_reason": "active_attempt",
            "next_safe_action": ("wait for the active attempt to "
                                 "finish (ada_status); re-arm with "
                                 "ada_card_action do=retry only after "
                                 "it ends"),
            "mutations_performed": [],
        }, ensure_ascii=False, indent=2)
    payload, problems, notes = _build_staged_action(card, action or {})
    missing = _dispatch_missing(payload)
    cid = str(card.get("id") or "")
    if not confirmed:
        return json.dumps({
            "ok": not problems and not missing,
            "operation": "envelope-stage",
            "preview": True,
            "card": {"id": card.get("id"), "ref": card.get("ref"),
                     "title": card.get("title")},
            "action_status": state,
            "action": payload,
            "missing_fields": missing,
            "problems": problems,
            "notes": notes,
            "warning": ("This writes the action envelope and queues "
                        "the card for dispatch. Show the user the "
                        "payload, then retry the identical call with "
                        "confirmed=true."),
            "mutations_performed": [],
        }, ensure_ascii=False, indent=2)
    if problems or missing:
        return json.dumps({
            "ok": False,
            "operation": "envelope-stage",
            "error": "envelope incomplete — fix and re-draft",
            "missing_fields": missing,
            "problems": problems,
            "mutations_performed": [],
        }, ensure_ascii=False, indent=2)
    body = {"id": cid,
            "title": str(card.get("title") or cid)[:160],
            "on_exists": "queue",
            "action": payload,
            "text": "dispatch envelope staged via ada-ocb"}
    try:
        res = _post_json("/card", body, timeout=90)
    except Exception as exc:
        return json.dumps({"ok": False, "operation": "envelope-stage",
                           "error": str(exc),
                           "mutations_performed": []})
    try:
        fresh = find_card(card_reference)
        verify_missing = _dispatch_missing(_card_action(fresh))
        v_state, _v_runner = _card_action_state(fresh)
    except Exception:
        verify_missing, v_state = None, None
    staged_ok = verify_missing == []
    return json.dumps({
        "ok": staged_ok,
        "operation": "envelope-stage",
        "card": {"id": card.get("id"), "ref": card.get("ref"),
                 "title": card.get("title")},
        "board_result": res,
        "action_status": v_state,
        "dispatch_check": {"ok": staged_ok,
                           "missing_fields": verify_missing},
        "notes": notes,
        "mutations_performed": ["card.action", "action.status=queued"],
        "next_safe_action": (
            "envelope staged and card queued — a runner claim is the "
            "next event; track with ada_status" if staged_ok else
            "board accepted the write but verification read-back was "
            "incomplete — check ada_status before retrying"),
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def ada_spend_summary() -> str:
    """Read the published LiteLLM spend ledger without provider credentials."""
    try:
        report = _mddb_post(
            "get",
            {"collection": "ada-cms-pages",
             "key": "llm-gateway-spend-ledger", "lang": "en"},
        )
        content = str(report.get("contentMd") or report.get("content") or "")
        updated_at = report.get("updatedAt")
        try:
            report_age_s = max(0, time.time() - float(updated_at))
        except (TypeError, ValueError):
            report_age_s = None
        total = re.search(r"\*\*Total 24h spend:\s*\$([0-9.]+)", content)
        rows = []
        for match in re.finditer(
            r"^\|\s*([^|]+)\s*\|\s*([^|]+)\s*\|\s*([0-9]+)\s*\|"
            r"\s*([0-9]+)\s*\|\s*\$([0-9.]+)\s*\|$",
            content,
            re.M,
        ):
            if match.group(1).strip().lower() in {"key", "–", "-"}:
                continue
            rows.append({
                "key": match.group(1).strip(),
                "model": match.group(2).strip(),
                "calls": int(match.group(3)),
                "tokens": int(match.group(4)),
                "spend_usd": float(match.group(5)),
            })
        return json.dumps({
            "ok": True,
            "operation": "spend-summary",
            "source": "ada-cms-pages/llm-gateway-spend-ledger",
            "report_updated_at": updated_at,
            "report_age_s": report_age_s,
            "stale": report_age_s is None or report_age_s > 86400,
            "total_24h_usd": float(total.group(1)) if total else None,
            "rows": rows,
            "secrets_read": False,
            "mutations_performed": [],
            "next_safe_action": "keep paid jobs held if the budget guard is exceeded",
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({
            "ok": False,
            "operation": "spend-summary",
            "error": str(exc),
            "source": "ada-cms-pages/llm-gateway-spend-ledger",
            "secrets_read": False,
            "mutations_performed": [],
        }, ensure_ascii=False, indent=2)


@mcp.tool()
def ada_self_test() -> str:
    """Run the fixed local Ada bridge contract test without external inputs.

    This is intentionally narrower than a general test runner: it cannot
    accept a command, mutate the board, or select a repository path.
    """
    command = ["python3", "test_ada_mcp.py"]
    try:
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run(
            command,
            cwd=WORKSPACE,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (result.stdout + result.stderr).strip()[-2000:]
        return json.dumps({
            "ok": result.returncode == 0,
            "operation": "self-test",
            "command": command,
            "workspace": str(WORKSPACE),
            "exit_code": result.returncode,
            "output": output,
            "mutations_performed": [],
            "next_safe_action": "review the test receipt; no dispatch or mutation was performed",
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({
            "ok": False,
            "operation": "self-test",
            "command": command,
            "workspace": str(WORKSPACE),
            "error": str(exc),
            "mutations_performed": [],
        }, ensure_ascii=False, indent=2)


@mcp.tool()
def ada_debug_opencode() -> str:
    """Diagnose workspace, command files, MCP bridge, board reachability, and git state."""
    checks: dict[str, Any] = {
        "workspace": str(WORKSPACE),
        "command_file": (WORKSPACE / ".opencode/commands/ada.md").exists(),
        "config_file": (WORKSPACE / ".opencode/opencode.json").exists(),
        "bridge_file": (WORKSPACE / ".opencode/ada_mcp.py").exists(),
        "board_api": BOARD_API,
    }
    try:
        cards = _compact_cards()
        checks["board_reachable"] = True
        checks["ada_1010_present"] = any(c.get("id") == "ada-dev-control-plane-envelope" or c.get("ref") == "ADA-1010" for c in cards)
    except Exception as exc:
        checks["board_reachable"] = False
        checks["board_error"] = str(exc)
    code, output = _run(["git", "status", "--short"])
    checks["git_status_exit"] = code
    checks["git_status"] = output
    return json.dumps({"ok": checks.get("command_file") and checks.get("config_file") and checks.get("bridge_file"), "operation": "debug", "checks": checks}, ensure_ascii=False, indent=2)


@mcp.tool()
def ada_wiki_extensions() -> str:
    """List the approved MediaWiki extension state through board-api."""
    try:
        result = _post_json(
            "/wiki/extension",
            {"action": "list", "from": "ada"},
            timeout=15,
        )
        return json.dumps(
            {"ok": True, "operation": "wiki_extensions", "result": result,
             "mutations": []},
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        return json.dumps(
            {"ok": False, "operation": "wiki_extensions", "error": str(exc),
             "mutations": []},
            ensure_ascii=False,
            indent=2,
        )


WIKI_EXTENSION_ALLOWLIST = {
    "CategoryTree", "Mermaid", "MsUpload", "MultimediaViewer",
    "Cargo", "PageForms", "Lockdown",
}


@mcp.tool()
def ada_wiki_extension(
    action: str,
    name: str = "",
    confirmed: bool = False,
) -> str:
    """List or confirm-gated install/enable/disable of Wiki extensions."""
    action = action.strip().lower()
    name = name.strip()
    actions = {"list", "install", "enable", "disable"}
    if action not in actions:
        return json.dumps({
            "ok": False,
            "operation": "wiki_extension",
            "error": "action must be list|install|enable|disable",
        }, ensure_ascii=False, indent=2)
    if action != "list" and name not in WIKI_EXTENSION_ALLOWLIST:
        return json.dumps({
            "ok": False,
            "operation": "wiki_extension",
            "error": f"name is not approved: {sorted(WIKI_EXTENSION_ALLOWLIST)}",
            "mutations": [],
        }, ensure_ascii=False, indent=2)
    if action != "list" and not confirmed:
        return json.dumps({
            "ok": False,
            "operation": "wiki_extension",
            "needs_confirmation": True,
            "action": action,
            "name": name,
            "warning": (
                "This changes the live MediaWiki installation. "
                "Ask Tony for explicit confirmation, then retry the "
                "identical call with confirmed=true."
            ),
            "mutations": [],
        }, ensure_ascii=False, indent=2)
    try:
        payload = {"action": action, "from": "ada"}
        if name:
            payload["name"] = name
        result = _post_json("/wiki/extension", payload, timeout=90)
        return json.dumps({
            "ok": True,
            "operation": "wiki_extension",
            "result": result,
            "mutations": [] if action == "list" else [action],
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({
            "ok": False,
            "operation": "wiki_extension",
            "error": str(exc),
            "mutations": [],
        }, ensure_ascii=False, indent=2)


def _open_requests(card: dict) -> list:
    return [r for r in (card.get("requests") or [])
            if isinstance(r, dict) and r.get("status") != "answered"]


@mcp.tool()
def ada_attention(oldest: int = 5) -> str:
    """List what needs Tony: cards with unanswered requests, plus the N oldest backlog cards for sweep triage."""
    cards = _backlog_cards()
    pending = []
    for c in cards:
        open_reqs = _open_requests(c)
        if open_reqs:
            pending.append({
                "id": c.get("id"), "ref": c.get("ref"), "title": c.get("title"),
                "column": c.get("column"), "priority": c.get("priority"),
                "requests": [{"id": r.get("id"), "ask": r.get("ask"),
                              "options": r.get("options") or [],
                              "suggested": r.get("suggested")} for r in open_reqs],
            })
    backlog = [c for c in cards if c.get("column") == "backlog"]
    backlog.sort(key=lambda c: c.get("updated") or "")
    oldest_cards = [{
        "id": c.get("id"), "ref": c.get("ref"), "title": c.get("title"),
        "priority": c.get("priority"), "updated": c.get("updated"),
        "action": c.get("action"),
    } for c in backlog[:max(0, int(oldest))]]
    return json.dumps({
        "ok": True, "operation": "attention",
        "pending_requests": pending,
        "oldest_backlog": oldest_cards,
        "next": "present each pending request as a native question; answers go through ada_respond",
        "mutations": [],
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def ada_respond(card_reference: str, request_id: str, answer: str, confirmed: bool = False) -> str:
    """Answer an open card request on the board -- mutation, two-call confirmation gate."""
    try:
        card = find_card(card_reference)
    except Exception as exc:
        return json.dumps({"ok": False, "operation": "respond", "error": str(exc), "mutations": []})
    open_reqs = _open_requests(card)
    target = next((r for r in open_reqs if r.get("id") == request_id), None)
    if target is None:
        return json.dumps({"ok": False, "operation": "respond",
                           "error": "no open request with that id on this card",
                           "open_request_ids": [r.get("id") for r in open_reqs],
                           "mutations": []})
    if not confirmed:
        return json.dumps({"ok": False, "operation": "respond", "preview": {
            "card": card.get("id"), "request_id": request_id,
            "ask": target.get("ask"), "answer": answer},
            "warning": "This writes the answer to the board. Retry the identical call with confirmed=true.",
            "mutations": []})
    try:
        result = _post_json("/respond", {"id": card.get("id"), "request_id": request_id,
                                         "answer": answer, "from": "ada"}, timeout=30)
        return json.dumps({"ok": True, "operation": "respond", "result": result,
                           "mutations": ["respond"]})
    except Exception as exc:
        return json.dumps({"ok": False, "operation": "respond", "error": str(exc), "mutations": []})


_CARD_ACTIONS = {"queue", "hold"}


@mcp.tool()
def ada_card_action(card_reference: str, do: str, confirmed: bool = False) -> str:
    """Queue or hold a board card -- mutation, two-call confirmation gate.
    This does NOT stage a dispatch envelope: action.lane_envelope and the
    executor fields are written only by ada_envelope_draft +
    ada_envelope_stage. Queue is refused while the card is queued or
    running — a live attempt must finish first."""
    if do not in _CARD_ACTIONS:
        return json.dumps({"ok": False, "operation": "card_action",
                           "error": f"action {do!r} not allowed -- permitted: {sorted(_CARD_ACTIONS)}",
                           "mutations": []})
    try:
        card = find_card(card_reference)
    except Exception as exc:
        return json.dumps({"ok": False, "operation": "card_action", "error": str(exc), "mutations": []})
    state, runner = _card_action_state(card)
    if do == "queue" and state in ("queued", "running"):
        return json.dumps({"ok": False, "operation": "card_action",
                           "card": {"id": card.get("id"), "ref": card.get("ref"),
                                    "title": card.get("title")},
                           "action_status": state,
                           "runner": runner or None,
                           "error": f"card already {state}"
                                    + (f" on {runner}" if runner else "")
                                    + " — queue refused; a live attempt "
                                    "must finish first",
                           "blocked_reason": "active_attempt",
                           "next_safe_action": ("track with ada_status; "
                                                "re-arm with do=retry "
                                                "only after the attempt "
                                                "ends"),
                           "mutations": []})
    if not confirmed:
        return json.dumps({"ok": False, "operation": "card_action", "preview": {
            "card": card.get("id"), "do": do, "current_action": card.get("action"),
            "action_status": state, "runner": runner or None,
            "warning": "This mutates board action state. Retry the identical call with confirmed=true.",
            "mutations": []}})
    try:
        result = _post_json("/action", {"id": card.get("id"), "do": do}, timeout=30)
        return json.dumps({"ok": True, "operation": "card_action", "result": result,
                           "mutations": [do]})
    except Exception as exc:
        return json.dumps({"ok": False, "operation": "card_action", "error": str(exc), "mutations": []})


ADOBE_PICKER = os.environ.get(
    "ADA_ADOBE_PICKER", "http://100.68.142.13:8777").rstrip("/")
_QC_DECISIONS = ("keep", "reject", "revise")
_ADOBE_CAMPAIGN = "AS-2026-11-05-REMOTE-WORK-001"


def _picker_get(path: str, timeout: float = 30) -> Any:
    req = urllib.request.Request(
        f"{ADOBE_PICKER}{path}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def _picker_post(path: str, payload: dict[str, Any], timeout: float = 30) -> Any:
    req = urllib.request.Request(
        f"{ADOBE_PICKER}{path}",
        data=json.dumps(payload).encode(),
        headers={"Accept": "application/json",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


@mcp.tool()
def ada_adobe_stock_qc() -> str:
    """Adobe Stock campaign STATUS/QC — call FIRST for 'adobe stock status',
    candidate review, or repair. Returns pending candidate image + QC menu spec.

    Read-only. Show the candidate `markdown` (renders the image inline),
    then present a native question menu with the `menu` options. Tony's
    pick becomes the asset+decision for ada_adobe_stock_qc_decide
    (keep|reject|revise); a 'repair' pick means reply with `picker_url`
    instead — no decide call.
    """
    try:
        camp = _picker_get("/api/campaign", 60)
        sess = _picker_get("/api/session", 60)
        assets = camp.get("generated_files") or []
        focus = ((sess.get("comparison") or {})
                 .get("last_landed") or {}).get("asset_id") \
            or (assets[-1] if assets else None)
        out: dict[str, Any] = {
            "ok": True, "operation": "adobe-stock-qc",
            "campaign": _ADOBE_CAMPAIGN,
            "status": camp.get("status"), "focus_asset": focus,
            "pending_decisions": camp.get("pending_decisions") or [],
            "adobe_upload": False,
        }
        if focus:
            out["image_url"] = f"{ADOBE_PICKER}/img/{focus}"
            out["picker_url"] = f"{ADOBE_PICKER}/?img=/img/{focus}"
            out["markdown"] = (
                f"**Candidate: {focus}**\n\n"
                f"![{focus}]({ADOBE_PICKER}/img/{focus})\n")
            out["menu"] = [
                {"id": f"adobe_qc:{d}:{focus}",
                 "label": f"{d.capitalize()} {focus}"}
                for d in _QC_DECISIONS
            ] + [{"id": f"adobe_qc:repair:{focus}",
                  "label": f"Repair region… ({focus})"}]
        out["next_safe_action"] = (
            "show the candidate image markdown, then present a native "
            "question menu with the `menu` options; on Tony's pick call "
            "ada_adobe_stock_qc_decide with that asset + decision and "
            "selection_confirmation=asset — for 'repair' reply with "
            "picker_url instead of deciding")
        return json.dumps(out, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"ok": False, "operation": "adobe-stock-qc",
                           "error": str(exc)})


@mcp.tool()
def ada_adobe_stock_qc_decide(
        asset: str, decision: str, note: str = "",
        selection_confirmation: str = "") -> str:
    """Record Tony's QC decision for a candidate — mutation, native-menu gate.

    Writes SESSION.json + appends 40-qc-review.md on Drive via the
    picker-server. Requires selection_confirmation == asset after the
    native confirmation menu pick; keep|reject|revise only.
    """
    if selection_confirmation.strip().lower() != asset.strip().lower():
        return json.dumps({
            "ok": False, "operation": "adobe-stock-qc-decide",
            "error": "selection_confirmation_required",
            "expected": asset,
            "next_safe_action": "show a native confirmation menu and "
                                "retry with the exact selected asset"})
    dec = decision.strip().lower()
    if dec not in _QC_DECISIONS:
        return json.dumps({"ok": False, "operation": "adobe-stock-qc-decide",
                           "error": f"decision must be one of {_QC_DECISIONS}"})
    try:
        res = _picker_post("/api/qc-decision",
                           {"asset": asset, "decision": dec,
                            "note": (note or "")[:400]}, timeout=120)
        return json.dumps({"ok": bool(res.get("ok")),
                           "operation": "adobe-stock-qc-decide",
                           "result": res,
                           "recorded": {"asset": asset, "decision": dec}},
                          ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"ok": False,
                           "operation": "adobe-stock-qc-decide",
                           "error": str(exc)})


def _safe_cwd() -> str:
    try:
        return os.getcwd()
    except FileNotFoundError:
        return ""


def _bridge_meta() -> dict[str, Any]:
    """The top-level 'bridge' object attached to every tool response."""
    manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(manager, "_tools", None) if manager else None
    return {
        "version": BRIDGE_VERSION,
        "tools_count": len(tools) if isinstance(tools, dict) else 0,
    }


def _with_bridge_envelope(result: Any) -> Any:
    """Attach the top-level bridge object to a JSON tool response."""
    if isinstance(result, dict):
        return {"bridge": _bridge_meta(), **result}
    if not isinstance(result, str):
        return result
    try:
        parsed = json.loads(result)
    except Exception:
        return result
    if not isinstance(parsed, dict):
        return result
    return json.dumps({"bridge": _bridge_meta(), **parsed},
                      ensure_ascii=False, indent=2)


def _audit_log(tool_name: str, kwargs: dict[str, Any], result: Any,
               elapsed: float, error: str = "") -> None:
    """Append one JSONL line per MCP tool call for session debugging."""
    try:
        audit_dir = _audit_dir()
        audit_dir.mkdir(parents=True, exist_ok=True)
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "bridge_version": BRIDGE_VERSION,
            "tool": tool_name,
            "cwd": _safe_cwd(),
            "elapsed_s": round(elapsed, 3),
            "args": {
                k: (v if isinstance(v, (int, float, bool, type(None))) else str(v)[:200])
                for k, v in kwargs.items()
            },
        }
        if error:
            entry["error"] = error[:300]
        else:
            text = result if isinstance(result, str) else json.dumps(result, default=str)
            entry["result_preview"] = str(text)[:500]
            try:
                parsed = json.loads(text) if isinstance(text, str) else result
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                entry["ok"] = parsed.get("ok")
                if parsed.get("errors"):
                    entry["errors"] = parsed["errors"]
        with open(audit_dir / "calls.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


@mcp.tool()
def ada_bridge_info() -> str:
    """Report which bridge copy is serving: version, tool list, features.

    Two ada_mcp.py copies at different revisions return different
    versions — use this to prove which copy served a session.
    """
    manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(manager, "_tools", None) if manager else None
    tool_names = sorted(tools) if isinstance(tools, dict) else []
    return json.dumps({
        "ok": True,
        "operation": "bridge-info",
        "bridge": {
            "version": BRIDGE_VERSION,
            "tools_count": len(tool_names),
            "tools": tool_names,
        },
        "features": {
            "response_bridge_envelope": True,
            "call_audit_jsonl": str(_audit_dir() / "calls.jsonl"),
            "version_resolution": (
                "ADA_BRIDGE_VERSION env > .opencode/VERSION file > "
                "git describe --always --dirty > content hash"
            ),
        },
        "workspace": str(WORKSPACE),
        "board_api": BOARD_API,
    }, ensure_ascii=False, indent=2)


def _install_call_audit() -> None:
    """Wrap every registered MCP tool with the JSONL call audit."""
    import inspect as _inspect

    manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(manager, "_tools", None) if manager else None
    if not isinstance(tools, dict):
        return
    for name, tool in list(tools.items()):
        fn = getattr(tool, "fn", None)
        if fn is None or getattr(fn, "_ada_audited", False):
            continue
        if _inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def _aw(*args, _fn=fn, _name=name, **kwargs):
                t0 = time.monotonic()
                try:
                    res = _with_bridge_envelope(await _fn(*args, **kwargs))
                except Exception as exc:
                    _audit_log(_name, kwargs, None, time.monotonic() - t0, error=str(exc))
                    raise
                _audit_log(_name, kwargs, res, time.monotonic() - t0)
                return res
            tool.fn = _aw
        else:
            @functools.wraps(fn)
            def _sw(*args, _fn=fn, _name=name, **kwargs):
                t0 = time.monotonic()
                try:
                    res = _with_bridge_envelope(_fn(*args, **kwargs))
                except Exception as exc:
                    _audit_log(_name, kwargs, None, time.monotonic() - t0, error=str(exc))
                    raise
                _audit_log(_name, kwargs, res, time.monotonic() - t0)
                return res
            tool.fn = _sw
        tool.fn._ada_audited = True


_install_call_audit()


def _audit_startup() -> None:
    """Record which bridge copy spawned so sessions have version evidence."""
    try:
        audit_dir = _audit_dir()
        audit_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "event": "startup",
            "bridge_version": BRIDGE_VERSION,
            "tools_count": _bridge_meta()["tools_count"],
            "cwd": _safe_cwd(),
            "pid": os.getpid(),
        }
        with open(audit_dir / "calls.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    _audit_startup()
    print(
        f"ada-control bridge {BRIDGE_VERSION} starting "
        f"(tools={_bridge_meta()['tools_count']} pid={os.getpid()} cwd={_safe_cwd()})",
        file=sys.stderr,
    )
    mcp.run(transport="stdio")
