"""Scenario test engine for Ada's memory/tooling layer.

Two execution surfaces share this module:

- tests/test_scenarios.py runs every tests/scenarios/*.yaml offline against
  a ToolRunner wired to an in-memory FakeMddb — deterministic, no network.
- scripts/scenario-live.py drives real /ws text turns against a live service
  and applies the same expectation vocabulary to ws trace events.

A scenario file looks like:

    name: michael-technician
    instance: michael
    seed:
      - bank: home
        key: runbook/ada-ha-michael
        text: "ada-ha-michael runs on mn01:8003; restart with
               systemctl --user restart ada-ha-michael"
        meta: {kind: [procedure], subject: [ada-ha-michael]}
    steps:
      - tool: ada_memory_search
        args: {query: "ada-ha-michael dashboard not loading"}
        expect: {count_min: 1, hits_contain: ["restart"]}
      - say: "it's the ada-ha-michael one"          # feeds conversation ctx
      - expand_query: "the other one"
        expect: {expanded_contains: ["ada-ha-michael"]}
      - reconnect_after: 3h          # simulate ws disconnect+reconnect;
        expect: {output_contains: ['"tier": "return"']}   # null = first session

Steps may also carry `capture: {name: "regex"}` — the first regex group is
stored and `{name}` placeholders in later steps' args are substituted
(e.g. replay a confirm_token minted by an earlier denial). `audit:
confirmations` snapshots the runner's structured confirm ledger.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from backend import board_client as board_client_mod
from backend import devin_dispatch as devin_dispatch_mod
from backend import lane_budget
from backend import memory_ops
from backend.conversation_memory import ConversationMemory
from backend.home_assistant import HomeAssistantClient
from backend.memory_banks import MemoryBankRegistry
from backend.tool_runner import ToolRunner
from backend.usage_tracker import usage_ledger

# Registry mirror of the deployed bank set (ssot.apps.ada-memory-banks.yml),
# trimmed to what the tools exercise. Scenario files may override with a
# `banks:` block of their own.
SCENARIO_BANKS: dict[str, Any] = {
    "banks": {
        "general": {
            "title": "General knowledge",
            "scope": "shared",
            "instances": ["tony", "michael"],
            "mddb_collection": "ada-ha-bank-general",
            "notebooklm_group": "memory",
            "kinds": ["fact", "preference", "note"],
            "writable": True,
            "write_policy": "confirmed",
            "allowed_tools": ["ada_remember", "ada_forget", "ada_outcome"],
            "status": "active",
        },
        "home": {
            "title": "Household knowledge",
            "scope": "shared",
            "instances": ["tony", "michael"],
            "mddb_collection": "ada-ha-bank-home",
            "kinds": ["fact", "procedure", "note"],
            "writable": False,
            "write_policy": "confirmed",
            "allowed_tools": [],
            "status": "active",
        },
        "devin": {
            "title": "Devin session archive",
            "scope": "instance",
            "instances": ["tony"],
            "mddb_collection": "ada-ha-bank-devin-{instance}",
            "kinds": ["report", "session-dump", "session-summary", "note"],
            "writable": False,
            "write_policy": "confirmed",
            "allowed_tools": [],
            "status": "active",
        },
        "infrastructure-ssot": {
            "title": "Infrastructure SSOT",
            "scope": "shared",
            "instances": ["tony", "michael"],
            "mddb_collection": "infrastructure-ssot",
            "kinds": ["fact", "procedure", "note"],
            "writable": False,
            "write_policy": "confirmed",
            "allowed_tools": [],
            "status": "active",
        },
        "people": {
            "title": "People",
            "scope": "shared",
            "instances": ["tony", "michael"],
            "mddb_collection": "ada-ha-bank-people",
            "kinds": ["person", "fact", "note"],
            "writable": True,
            "write_policy": "confirmed",
            "allowed_tools": ["ada_remember", "ada_forget", "ada_outcome"],
            "status": "active",
        },
        "personal": {
            "title": "Personal",
            "scope": "instance",
            "instances": ["tony", "michael"],
            "mddb_collection": "ada-ha-bank-personal-{instance}",
            "kinds": ["fact", "preference", "person", "procedure", "note"],
            "writable": True,
            "write_policy": "confirmed",
            "allowed_tools": ["ada_remember", "ada_forget", "ada_outcome"],
            "status": "active",
        },
        "note": {
            "title": "Scratch notes (test bank)",
            "scope": "instance",
            "instances": ["tony"],
            "mddb_collection": "ada-ha-bank-note-{instance}",
            "kinds": ["note", "fact"],
            "writable": True,
            "write_policy": "direct",
            "allowed_tools": ["ada_remember", "ada_forget", "ada_outcome"],
            "status": "testing",
        },
        "tony-projects": {
            "title": "Tony's projects",
            "scope": "instance",
            "instances": ["tony"],
            "mddb_collection": "ada-ha-bank-projects-tony",
            "kinds": ["note", "fact", "procedure"],
            "writable": True,
            "write_policy": "confirmed",
            "allowed_tools": ["ada_remember", "ada_forget", "ada_outcome"],
            "status": "active",
        },
    }
}

_AWAY_RE = re.compile(r"^(\d+(?:\.\d+)?)(s|m|h|d|w)?$")
_AWAY_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _parse_away(raw: Any) -> float | None:
    """'90s'/'5m'/'4h'/'2d'/'1w' or bare seconds; null/'none' = first session."""
    if raw is None or str(raw).strip().lower() in ("", "none", "null"):
        return None
    m = _AWAY_RE.match(str(raw).strip().lower())
    if not m:
        raise ValueError(f"bad reconnect_after duration {raw!r}")
    return float(m.group(1)) * _AWAY_UNITS.get(m.group(2) or "s", 1)


_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "a", "an", "is", "it", "to", "of", "and", "or", "in", "on",
    "what", "who", "how", "do", "does", "for", "about", "me", "my", "i",
}


class FakeMddb:
    """In-memory MDDB: real add/get/update semantics plus a keyword-overlap
    'vector' search so scenario steps exercise the genuine find-then-update
    and draft-filtering logic in memory_ops."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, dict[str, Any]]] = {}

    def is_ops_routed(self, collection: str) -> bool:
        # MddbClient marks ops/scenario-report collections as ops-routed so
        # status_outcome docs skip the 'active' status gate — the fake has
        # no such routing, plain collections only.
        return False

    def _coll(self, name: str) -> dict[str, dict[str, Any]]:
        return self.collections.setdefault(name, {})

    async def add_document(self, collection, key, lang, content_md, meta=None, timeout=None,
                           durable=False, tool="", session_id=None):
        self._coll(collection)[str(key)] = {
            "key": str(key),
            "contentMd": str(content_md),
            "meta": dict(meta or {}),
        }
        return {"key": str(key)}

    async def get_document(self, collection, key, lang="en"):
        doc = self._coll(collection).get(str(key))
        return dict(doc) if doc else None

    async def update_document(self, collection, key, lang="en", content_md=None, meta=None,
                              durable=False, tool="", session_id=None):
        doc = self._coll(collection).get(str(key))
        if doc is None:
            return None
        if content_md is not None:
            doc["contentMd"] = str(content_md)
        if meta is not None:
            doc["meta"] = dict(meta)
        return {"key": str(key)}

    async def delete_document(self, collection, key, lang="en",
                              durable=False, tool="", session_id=None):
        return ({"key": str(key)}
                if self._coll(collection).pop(str(key), None) is not None
                else None)

    @staticmethod
    def _meta_match(doc: dict, filter_meta: dict[str, list[str]] | None) -> bool:
        if not filter_meta:
            return True
        meta = doc.get("meta") or {}
        for field, wanted in filter_meta.items():
            have = meta.get(field) or []
            if not isinstance(have, list):
                have = [have]
            if not set(map(str, wanted)) & set(map(str, have)):
                return False
        return True

    async def search_documents(self, collection, query="*", filter_meta=None, limit=10):
        docs = [
            dict(d) for d in self._coll(collection).values()
            if self._meta_match(d, filter_meta)
        ]
        return docs[: int(limit)]

    async def vector_search(self, collection, query, limit=5, filter_meta=None, threshold=0.0):
        terms = {
            t for t in _WORD_RE.findall(str(query).lower()) if t not in _STOP
        }
        out = []
        for d in self._coll(collection).values():
            if not self._meta_match(d, filter_meta):
                continue
            meta = d.get("meta") or {}
            haystack = " ".join(
                [
                    str(d.get("key") or ""),
                    str(d.get("contentMd") or ""),
                    " ".join(map(str, meta.get("subject") or [])),
                    " ".join(map(str, meta.get("attribute") or [])),
                ]
            ).lower()
            hay_terms = set(_WORD_RE.findall(haystack))
            score = len(terms & hay_terms) / len(terms) if terms else 0.0
            if score >= float(threshold or 0):
                out.append({**dict(d), "score": round(score, 3)})
        out.sort(key=lambda d: d["score"], reverse=True)
        return out[: int(limit)]


def _board_slug(text: Any) -> str:
    return re.sub(r"-+", "-",
                  re.sub(r"[^a-z0-9]+", "-",
                         str(text or "").lower())).strip("-")


_BOARD_COLUMNS = ("lab", "backlog", "doing", "review", "done")


class FakeBoard:
    """In-memory kanban board-api stand-in.

    Declared per scenario as `board: {cards: [...], doing_limit: n,
    columns: [...], unreachable: bool, errors: {"METHOD /path": {...}}}`.
    Every board-api call in the codebase funnels through
    board_client.request, so run_scenario patches that single seam — the
    real tool code (kanban, ada_opencode*, ada_sweep, wiki_extension,
    devin board dispatch) runs verbatim against this fixture. `calls`
    records every (method, path, body) for audit: board assertions.
    """

    def __init__(self, spec: dict[str, Any] | None = None) -> None:
        spec = spec or {}
        self.columns = [str(c) for c in
                        (spec.get("columns") or _BOARD_COLUMNS)]
        self.doing_limit = int(spec.get("doing_limit") or 2)
        self.unreachable = bool(spec.get("unreachable"))
        self.errors = {str(k): dict(v) for k, v in
                       (spec.get("errors") or {}).items()}
        self.cards: list[dict[str, Any]] = []
        for raw in spec.get("cards") or []:
            card = dict(raw)
            card.setdefault("id", _board_slug(card.get("title")) or "card")
            card.setdefault("column", "backlog")
            self.cards.append(card)
        self.calls: list[list[Any]] = []
        self._stamp_n = 0
        self._req_n = 0

    def _stamp(self) -> str:
        # Deterministic 'updated' bumps so the stale-write guard can tell
        # a moved card apart from the fixture's declared stamps.
        self._stamp_n += 1
        return f"2026-11-19 09:{self._stamp_n:02d}"

    def _find(self, cid: str) -> dict[str, Any] | None:
        for c in self.cards:
            if str(c.get("id")) == cid:
                return c
        return None

    async def handle(self, client: Any, method: str, path: str,
                     **kw: Any) -> tuple[dict[str, Any] | None,
                                         str | None, int]:
        """Drop-in for board_client.request -> (data, err, http_status)."""
        method = str(method).upper()
        path_only, _, query = str(path).partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        body = dict(kw.get("json") or {})
        self.calls.append([method, path_only, body])
        if self.unreachable:
            return None, "I couldn't reach the board (ConnectError)", 0
        fail = self.errors.get(f"{method} {path_only}")
        if fail is not None:
            status = int(fail.get("status") or 500)
            return None, str(fail.get("error") or f"HTTP {status}"), status

        if method == "GET" and path_only == "/cards":
            cards = self.cards
            col = str(params.get("column") or "")
            if col:
                cards = [c for c in cards
                         if str(c.get("column") or "backlog") == col]
            cid = str(params.get("id") or "")
            if cid:
                cards = [c for c in cards if str(c.get("id")) == cid]
            return ({"ok": True,
                     "cards": [dict(c) for c in cards],
                     "columns": [{"id": c} for c in self.columns],
                     "doing_limit": self.doing_limit}, None, 200)
        m = re.match(r"^/card/(.+)$", path_only)
        if method == "GET" and m:
            card = self._find(m.group(1)) or next(
                (c for c in self.cards
                 if str(c.get("ref") or "") == m.group(1)), None)
            if card is None:
                return None, f"no card {m.group(1)}", 404
            return {"ok": True, "card": dict(card)}, None, 200
        if method == "POST" and path_only == "/card":
            cid = str(body.get("id") or _board_slug(body.get("title")))
            card = self._find(cid)
            if card is not None:
                card.update(
                    {k: v for k, v in body.items() if k != "on_exists"})
                if body.get("queue") or body.get("on_exists") == "queue":
                    card.setdefault("action", {})["status"] = "queued"
                card["updated"] = self._stamp()
                return ({"ok": True, "id": cid,
                         "message": f"card {cid} updated",
                         "card": dict(card)}, None, 200)
            card = dict(body)
            card.update({
                "id": cid,
                "title": str(body.get("title") or cid)[:160],
                "column": str(body.get("column") or "backlog"),
                "updated": self._stamp(),
                "comms": [{"at": self._stamp(),
                           "from": str(body.get("from") or "ada"),
                           "text": str(body.get("text") or "card filed")}],
                "requests": [],
            })
            if body.get("queue"):
                card.setdefault("action", {})["status"] = "queued"
            card.pop("on_exists", None)
            card.pop("queue", None)
            self.cards.append(card)
            return ({"ok": True, "id": cid,
                     "message": f"card {cid} created",
                     "card": dict(card)}, None, 200)
        if method == "POST" and path_only == "/comment":
            card = self._find(str(body.get("id") or ""))
            if card is None:
                return None, f"no card {body.get('id')}", 404
            card.setdefault("comms", []).append({
                "at": self._stamp(),
                "from": str(body.get("from") or "ada"),
                "text": str(body.get("text") or "")})
            card["updated"] = self._stamp()
            return {"ok": True, "message": "comment added"}, None, 200
        if method == "POST" and path_only == "/request":
            card = self._find(str(body.get("id") or ""))
            if card is None:
                return None, f"no card {body.get('id')}", 404
            self._req_n += 1
            req = {"id": f"r{self._req_n}", "status": "open",
                   "from": str(body.get("from") or "ada"),
                   "ask": str(body.get("ask") or "")}
            if body.get("options"):
                req["options"] = body["options"]
            if body.get("suggested"):
                req["suggested"] = body["suggested"]
            card.setdefault("requests", []).append(req)
            card["updated"] = self._stamp()
            return ({"ok": True, "message": "request added",
                     "request_id": req["id"]}, None, 200)
        if method == "POST" and path_only == "/respond":
            card = self._find(str(body.get("id") or ""))
            if card is None:
                return None, f"no card {body.get('id')}", 404
            rid = str(body.get("request_id") or "")
            req = next((r for r in (card.get("requests") or [])
                        if str(r.get("id")) == rid), None)
            if req is None:
                return None, f"no request {rid}", 404
            req["status"] = "answered"
            req["answer"] = str(body.get("answer") or "")
            card.setdefault("comms", []).append({
                "at": self._stamp(), "from": "tony",
                "text": f"answered {rid}: {req['answer'][:200]}"})
            card["updated"] = self._stamp()
            return {"ok": True, "message": "answer saved"}, None, 200
        if method == "POST" and path_only == "/action":
            card = self._find(str(body.get("id") or ""))
            if card is None:
                return None, f"no card {body.get('id')}", 404
            do = str(body.get("do") or "")
            if do == "move":
                target = str(body.get("column") or "")
                if target not in self.columns:
                    return (None, f"no column {target}", 400)
                card["column"] = target
            elif do == "queue":
                card.setdefault("action", {})["status"] = "queued"
            elif do == "park":
                card.setdefault("action", {})["status"] = "parked"
            else:
                return None, f"unknown action {do!r}", 400
            card["updated"] = self._stamp()
            return {"ok": True, "message": f"{do} ok"}, None, 200
        if method == "POST" and path_only == "/wiki/extension":
            action = str(body.get("action") or "list")
            if action == "list":
                return ({"ok": True, "action": "list",
                         "extensions": ["CategoryTree", "Mermaid",
                                        "MsUpload"]}, None, 200)
            return ({"ok": True, "action": action,
                     "name": body.get("name"),
                     "message": f"{action} {body.get('name')} ok"},
                    None, 200)
        return None, f"fakeboard: unhandled {method} {path_only}", 404


class _FakeProc:
    """Subprocess stand-in for the dispatch_ssh fixture."""

    def __init__(self, out: bytes = b"", err: bytes = b"",
                 rc: int = 0) -> None:
        self._out, self._err, self.returncode = out, err, rc
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._out, self._err

    def kill(self) -> None:
        self.killed = True
        self.returncode = self.returncode or -9

    async def wait(self) -> int:
        return self.returncode


def build_registry(instance: str, spec: dict | None = None) -> MemoryBankRegistry:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(spec or SCENARIO_BANKS, f)
        path = f.name
    reg = MemoryBankRegistry(path=path, instance=instance, notebook_ids={"memory": "nb-1"})
    Path(path).unlink(missing_ok=True)
    return reg


def check_expect(result: Any, expect: dict[str, Any]) -> list[str]:
    """Evaluate expectation keys against a step result; returns failures."""
    failures: list[str] = []
    result = result if isinstance(result, dict) else {"output": result}
    blob = json.dumps(result, default=str)
    hits = result.get("hits") or []

    if "verb" in expect and result.get("verb") != expect["verb"]:
        failures.append(f"verb: want {expect['verb']!r}, got {result.get('verb')!r}")
    # Contract (tool-error-contract): execute() results carry top-level
    # ok: bool — no_error trips on ok:false even when no error text came
    # back (e.g. a not_found/buried failure normalized at the boundary).
    if expect.get("no_error") and (
            result.get("error") or result.get("ok") is False):
        failures.append(
            f"unexpected error: {result.get('error') or result}")
    if "error_contains" in expect and expect["error_contains"] not in str(result.get("error", "")):
        failures.append(f"error: want substring {expect['error_contains']!r}, got {result.get('error')!r}")
    if "count" in expect and result.get("count") != expect["count"]:
        failures.append(f"count: want {expect['count']}, got {result.get('count')}")
    if "count_min" in expect and (result.get("count") or 0) < expect["count_min"]:
        failures.append(f"count: want >= {expect['count_min']}, got {result.get('count')}")
    for sub in expect.get("hits_contain") or []:
        if not any(sub in str(h.get("content") or "") for h in hits):
            failures.append(f"hits: no hit contains {sub!r}")
    for bank in expect.get("hit_banks") or []:
        if not any(h.get("bank") == bank for h in hits):
            failures.append(f"hits: no hit from bank {bank!r}")
    if "draft_hits_min" in expect:
        n = sum(1 for h in hits if h.get("draft"))
        if n < expect["draft_hits_min"]:
            failures.append(f"draft hits: want >= {expect['draft_hits_min']}, got {n}")
    for sub in expect.get("expanded_contains") or []:
        if sub not in str(result.get("expanded") or ""):
            failures.append(f"expanded query missing {sub!r}")
    for sub in expect.get("output_contains") or []:
        if sub not in blob:
            failures.append(f"output missing {sub!r}")
    for sub in expect.get("output_not_contains") or []:
        if sub in blob:
            failures.append(f"output unexpectedly contains {sub!r}")
    return failures


async def run_scenario(path: str | Path) -> dict[str, Any]:
    """Execute one scenario file offline; returns a report dict."""
    raw_text = Path(path).read_text()
    # `__NOW__`/`__NOW_M_<secs>__` placeholders inline the current epoch —
    # freshness-sensitive fixtures (beacon ts, tracker last_changed ISO
    # is covered by __NOWISO__/variants below) need 'now', not literals.
    now_i = int(time.time())
    raw_text = re.sub(r"__NOW_M_(\d+)__",
                      lambda m: str(now_i - int(m.group(1))), raw_text)
    raw_text = raw_text.replace("__NOW__", str(now_i))
    now_dt = datetime.now(timezone.utc)
    raw_text = re.sub(
        r"__NOWISO_M_(\d+)__",
        lambda m: (now_dt - timedelta(
            seconds=int(m.group(1)))).isoformat(), raw_text)
    raw_text = raw_text.replace("__NOWISO__", now_dt.isoformat())
    data = yaml.safe_load(raw_text)
    instance = str(data.get("instance") or "test")
    registry = build_registry(instance, data.get("banks"))
    fake = FakeMddb()
    runner = ToolRunner(unittest.mock.AsyncMock(), instance_id=instance)
    runner._banks = registry
    runner.mddb = fake
    # memory was built with a real MddbClient before the fake landed —
    # repoint it too so confidence/safety loads stay in-memory (control
    # tools hit _ensure_confidence on every gated call).
    if runner.memory is not None:
        runner.memory.mddb = fake
    # `persons:` declares the HA people resolve_person()/persons() should
    # see — matches by entity_id, friendly name, or name slug.
    people = [dict(p) for p in (data.get("persons") or [])]
    if people:
        def _norm(s: Any) -> str:
            return re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")

        async def _resolve_person(name: Any) -> dict | None:
            n = str(name or "").strip().lower()
            for p in people:
                if n in (str(p.get("entity_id") or "").lower(),
                         str(p.get("name") or "").lower()):
                    return dict(p)
            for p in people:
                if f"person.{_norm(p.get('name'))}" == n:
                    return dict(p)
            return None

        async def _persons() -> list[dict]:
            return [dict(p) for p in people]

        runner.context.ha_client.resolve_person = _resolve_person
        runner.context.ha_client.persons = _persons

    # `env:` sets os.environ for the scenario duration (restored after).
    env_spec = {str(k): str(v) for k, v in (data.get("env") or {}).items()}
    # `cast_screens:` writes the registry to a temp file and points
    # ADA_CAST_SCREENS at it — screen ownership + the tv block.
    tmp_files: list[str] = []
    if data.get("cast_screens") is not None:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as f:
            json.dump(data["cast_screens"], f)
            tmp_files.append(f.name)
        env_spec["ADA_CAST_SCREENS"] = f.name
    # `usage_log:` writes fixture usage-ledger records to a temp jsonl and
    # points ADA_USAGE_LOG at it — lane_budget's monthly-spend scan reads
    # that file. Records without `ts` are stamped inside the current
    # billing period so spend-cap scenarios stay deterministic year-round.
    if "usage_log" in data:
        period = lane_budget.current_period()
        with tempfile.NamedTemporaryFile(
                "w", suffix=".jsonl", delete=False) as f:
            for rec in data.get("usage_log") or []:
                r = dict(rec)
                if not r.get("ts"):
                    r["ts"] = f"{period}-15T00:00:00+00:00"
                f.write(json.dumps(r) + "\n")
            tmp_files.append(f.name)
        env_spec["ADA_USAGE_LOG"] = f.name
        if "ADA_LANE_BUDGET_STATE" not in env_spec:
            with tempfile.NamedTemporaryFile(
                    "w", suffix=".json", delete=False) as f:
                f.write("{}")
                tmp_files.append(f.name)
            env_spec["ADA_LANE_BUDGET_STATE"] = f.name
        lane_budget.reset()
    saved_env = {k: os.environ.get(k) for k in env_spec}
    os.environ.update(env_spec)

    # `budget_state:` seeds the provider-cap latch file — "__PERIOD__"
    # -> current billing period, "__KEYFP__" -> the GEMINI_API_KEY
    # fingerprint lane_budget matches against. Written after env_spec
    # applies so the fingerprint sees the scenario's key.
    if "budget_state" in data:
        state_txt = json.dumps(data["budget_state"]).replace(
            "__PERIOD__", lane_budget.current_period()).replace(
            "__KEYFP__", lane_budget.key_fingerprint(
                os.environ.get("GEMINI_API_KEY") or ""))
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as f:
            f.write(state_txt)
            tmp_files.append(f.name)
        if "ADA_LANE_BUDGET_STATE" not in saved_env:
            saved_env["ADA_LANE_BUDGET_STATE"] = os.environ.get(
                "ADA_LANE_BUDGET_STATE")
        os.environ["ADA_LANE_BUDGET_STATE"] = f.name
        lane_budget.reset()

    # Attribute patches needing restore after the run — [(obj, attr, orig)].
    restores: list[tuple[Any, str, Any]] = []

    # `ha_entities:` stub ha_client.entities() — the controllable-device
    # list the confidence gate (_ensure_confidence) reads on every
    # control call. Needed for scenarios that EXECUTE control_entity.
    if "ha_entities" in data:
        ha_ents = [dict(e) for e in (data.get("ha_entities") or [])]
        runner.context.ha_client.entities = unittest.mock.AsyncMock(
            return_value=ha_ents)
        # search_entities runs the REAL scorer over the same fixture —
        # ghost-entity failures name the close matches it finds.
        real_ha = HomeAssistantClient()
        real_ha.entities = unittest.mock.AsyncMock(return_value=ha_ents)
        runner.context.ha_client.search_entities = real_ha.search_entities
        # set_power keeps the client's echo contract ({entity_id,state})
        # — the post-call verify reads get_state, not this echo.
        runner.context.ha_client.set_power = unittest.mock.AsyncMock(
            side_effect=lambda eid, on: {
                "entity_id": eid, "state": "on" if on else "off"})
    # `ha_404:` entity_ids GET /api/states/<id> answers 404 for — the
    # ghost-entity signature control_entity's existence check trips on.
    ha_404 = {str(e) for e in (data.get("ha_404") or [])}
    # `ha_states:` / `ha_states_seq:` stub ha_client.get_state —
    # entity_id -> HA state doc ({"state","attributes"}); seq entries are
    # consumed in call order, the last one repeats.
    ha_states = data.get("ha_states") or {}
    ha_seqs = {k: list(v) for k, v in
               (data.get("ha_states_seq") or {}).items()}
    if ha_states or ha_seqs or ha_404:
        async def _get_state(entity_id):
            eid = str(entity_id)
            if eid in ha_404:
                req = httpx.Request("GET", f"/api/states/{eid}")
                raise httpx.HTTPStatusError(
                    "not found", request=req,
                    response=httpx.Response(404, request=req))
            seq = ha_seqs.get(eid)
            if seq:
                return dict(seq.pop(0) if len(seq) > 1 else seq[0])
            st = ha_states.get(eid)
            if isinstance(st, dict):
                return dict(st)
            return {"entity_id": entity_id, "state": "unknown"}
        runner.context.ha_client.get_state = _get_state
    # `ha_raw_states:` full /api/states list — stubs ha_client._states()
    # for tools that enumerate entities (device_tracker.*, sensors).
    if "ha_raw_states" in data:
        raw_states = list(data.get("ha_raw_states") or [])
        runner.context.ha_client._states = unittest.mock.AsyncMock(
            return_value=raw_states)
    # `ha_logbook:` {entity_id: [entries]} — stubs ha_client.logbook.
    if "ha_logbook" in data:
        lb_map = dict(data.get("ha_logbook") or {})

        async def _logbook(entity_id=None, hours=24, end=None):
            return list(lb_map.get(str(entity_id)) or [])
        runner.context.ha_client.logbook = _logbook
    # `ha_tv_action:` return value for ha_client.tv_action (the
    # cast-browser body).
    if "ha_tv_action" in data:
        tv_res = data["ha_tv_action"]
        runner.context.ha_client.tv_action = unittest.mock.AsyncMock(
            return_value=dict(tv_res) if isinstance(tv_res, dict)
            else tv_res)
    # `vcast:` stubs ToolRunner._vcast_api — path -> canned response.
    vcast_stub = data.get("vcast")
    if isinstance(vcast_stub, dict):
        def _fake_vcast(path, payload=None):
            resp = vcast_stub.get(path)
            if isinstance(resp, dict):
                if set(resp) == {"error"}:
                    raise RuntimeError(str(resp["error"]))
                return dict(resp)
            return resp if resp is not None else {}
        runner._vcast_api = _fake_vcast
    # `board:` in-memory kanban board — every board-api call in the
    # codebase funnels through board_client.request, so patching that one
    # seam runs the real tool code (kanban, ada_opencode*, ada_sweep,
    # wiki_extension, devin's card-dispatch path) against the fixture.
    # `audit: board` dumps the call log + final card state.
    fake_board: FakeBoard | None = None
    if "board" in data:
        fake_board = FakeBoard(
            data["board"] if isinstance(data["board"], dict) else {})
        restores.append((board_client_mod, "request",
                         board_client_mod.request))
        board_client_mod.request = fake_board.handle
    # `dispatch_ssh:` stubs the `ssh devin-dispatch ...` calls the playbook
    # dispatch path shells out to — {command: stdout} or
    # {command: {rc, out, err}} per _run subcommand (status/start/followup).
    # `audit: dispatch` dumps the intercepted argv list.
    dispatch_calls: list[list[str]] = []
    if "dispatch_ssh" in data:
        ssh_spec = {
            str(k): v for k, v in
            (data.get("dispatch_ssh") or {}).items()}

        async def _fake_exec(*a: Any, **kw: Any) -> _FakeProc:
            argv = [str(x) for x in a]
            remote = argv[-1] if argv else ""
            # remote_cmd = "<BIN> <args...>" — key on the subcommand.
            # `remote` stays whole so quoted args (the rendered task
            # text, e.g. 'Playbook: build-tool') audit contiguously.
            parts = remote.split()
            cmd = parts[1] if len(parts) > 1 else ""
            dispatch_calls.append({"argv": parts[1:], "remote": remote})
            spec = ssh_spec.get(cmd)
            if isinstance(spec, dict):
                return _FakeProc(
                    out=str(spec.get("out") or "").encode(),
                    err=str(spec.get("err") or "").encode(),
                    rc=int(spec.get("rc") or 0))
            return _FakeProc(out=str(spec or "").encode())

        restores.append(
            (asyncio, "create_subprocess_exec",
             asyncio.create_subprocess_exec))
        asyncio.create_subprocess_exec = _fake_exec  # type: ignore[assignment]
    conv = ConversationMemory(session_id="scenario")
    usage_ledger.configure(None)  # no data/usage.jsonl writes in offline runs
    usage_ledger.reset()
    # Module-level dispatch dedup cache — clear per scenario so one
    # scenario's dispatched tasks can't dedup a later scenario's call.
    devin_dispatch_mod._recent_dispatches.clear()
    today = datetime.now(timezone.utc).date().isoformat()

    for i, seed in enumerate(data.get("seed") or []):
        if "bank" in seed:
            bank = registry.bank(str(seed["bank"]))
            collection = bank.mddb_collection
            default_scope = instance if bank.scope == "instance" else "shared"
        else:
            collection = str(seed["collection"])
            default_scope = "shared"
        meta = {
            "status": ["active"],
            "valid_from": [today],
            "last_verified": [today],
            "scope": [default_scope],
            "kind": ["note"],
            "source": ["scenario"],
        }
        for k, v in (seed.get("meta") or {}).items():
            meta[k] = [today if str(x) == "__TODAY__" else x for x in (v if isinstance(v, list) else [v])]
        await fake.add_document(
            collection, str(seed.get("key") or f"seed/{i}"), "en",
            str(seed["text"]), meta,
        )

    report: dict[str, Any] = {
        "name": data.get("name") or Path(path).stem,
        "instance": instance,
        "steps": [],
        "failures": [],
    }
    captures: dict[str, str] = {}

    def _subst(value: Any) -> Any:
        if isinstance(value, str):
            for key, stored in captures.items():
                value = value.replace("{" + key + "}", stored)
            return value
        if isinstance(value, dict):
            return {k: _subst(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_subst(v) for v in value]
        return value

    def _restore_env() -> None:
        for obj, attr, orig in restores:
            setattr(obj, attr, orig)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for p in tmp_files:
            Path(p).unlink(missing_ok=True)

    try:
      for i, step in enumerate(data.get("steps") or []):
        entry: dict[str, Any] = {"i": i, "step": step}
        t0 = time.monotonic()
        try:
            if "say" in step:
                conv.add_user(str(step["say"]))
                result = {"output": "recorded"}
            elif "assistant" in step:
                conv.add_assistant(str(step["assistant"]))
                result = {"output": "recorded"}
            elif "expand_query" in step:
                result = {"expanded": conv.expand_query(str(step["expand_query"]))}
            elif "audit" in step:
                what = str(step["audit"])
                if what == "confirmations":
                    entries = runner.confirmation_audit()
                    result = {"count": len(entries), "entries": entries}
                elif what == "board":
                    result = ({
                        "calls": fake_board.calls,
                        "cards": fake_board.cards,
                    } if fake_board is not None else
                        {"error": "no board fixture in this scenario"})
                elif what == "dispatch":
                    result = {"count": len(dispatch_calls),
                              "calls": dispatch_calls}
                else:
                    result = {"error": f"unknown audit {step['audit']!r}"}
            elif "reconnect_after" in step:
                # Simulate a websocket disconnect+reconnect: snapshot the
                # transcript tail, start a fresh ConversationMemory, and run
                # the real session prime with the simulated gap.
                away = _parse_away(step["reconnect_after"])
                last_tail = conv.recent_context(max_turns=6, max_chars=800)
                conv = ConversationMemory(session_id=f"scenario-r{i}")
                result = {
                    "away_seconds": away,
                    "tier": memory_ops.away_tier(away),
                    "prime": await memory_ops.session_prime_text(
                        runner.mddb,
                        runner.banks,
                        away_seconds=away,
                        last_tail=last_tail,
                    ),
                }
            elif "set" in step:
                # Mutate runner session state — used by session-security
                # scenarios to pin the owner or swap the identified speaker.
                for k, v in dict(step["set"]).items():
                    setattr(runner, str(k), v)
                result = {"output": "set"}
            elif "record_usage" in step:
                usage_ledger.record(**dict(step["record_usage"]))
                result = {"output": "recorded"}
            elif "call" in step:
                # Direct call: a runner method name, or a dotted
                # 'module.function' for module-level helpers the model
                # surface can't reach (lane_budget.check, the playbook
                # dispatch path behind runner.devin_dispatch). Args are
                # kwargs. Exceptions wrap into the canonical error shape
                # so gate refusals are assertable like tool denials.
                target = str(step["call"])
                fn = getattr(runner, target, None)
                if fn is None and "." in target:
                    mod_name, _, attr = target.rpartition(".")
                    try:
                        fn = getattr(
                            __import__(mod_name, fromlist=[attr]), attr)
                    except Exception:
                        fn = None
                if not callable(fn):
                    result = {
                        "error": f"no callable {target!r} on the runner "
                                 "or as a dotted module path"}
                else:
                    try:
                        res = fn(**_subst(dict(step.get("args") or {})))
                        if hasattr(res, "__await__"):
                            res = await res
                        result = res
                    except Exception as exc:
                        result = {
                            "ok": False,
                            "error": f"{type(exc).__name__}: {exc}",
                            "error_type": type(exc).__name__,
                        }
            elif "tool" in step:
                try:
                    result = await runner.execute(
                        str(step["tool"]), _subst(dict(step.get("args") or {}))
                    )
                except Exception as exc:
                    # Gate denials still raise past the boundary — wrap
                    # them in the canonical shape so expectations and
                    # reports see one contract.
                    result = {
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "error_type": type(exc).__name__,
                    }
            else:
                result = {"error": "unknown step kind"}
        except Exception as exc:  # engine bug safety net
            result = {"error": f"engine: {exc}"}
        entry["ms"] = int((time.monotonic() - t0) * 1000)
        for var, pattern in (step.get("capture") or {}).items():
            match = re.search(str(pattern), json.dumps(result, default=str))
            if match:
                captures[var] = match.group(1)
        entry["result"] = result
        entry["failures"] = check_expect(result, step.get("expect") or {})
        report["steps"].append(entry)
        report["failures"].extend(
            f"step {i} ({step.get('tool') or next(iter(step))}): {f}"
            for f in entry["failures"]
        )
    finally:
        _restore_env()
    report["ok"] = not report["failures"]
    return report
