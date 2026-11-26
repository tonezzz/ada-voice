"""ada_fleet_jobs — the "how are jobs" report-verb read path.

Card ada-voice-fleet-jobs-verb; contract chaba
docs/kb/ada-report-verb-contract.md (producer/consumer split — the
fleet-jobs-report job on idc03 renders the page every 4h; this tool
only ever reads it).

Trigger intents: "how are jobs", "job status", "what is the fleet
doing", "are any timers failing", "which hosts are having trouble",
"how much did we spend", "what are the lanes doing", "is the dispatch
queue moving", "who's working on what".

Read path (the ONLY permitted path): one MDDB POST /get on
ada-cms-pages/fleet-jobs-report (lang en) -> {contentMd, meta}, then a
freshness gate on meta.updated vs meta.fresh_for (4h):

  fresh   -> answer normally from the rendered sections
  stale   -> same answer prefixed "as of <updated>" + a producer-may-
             be-down flag
  missing -> "the fleet jobs report hasn't been generated yet" + offer
             to card it

NEVER on a voice turn (contract): no aggregator run, no board-api
/cards or /queue scan, no psql/ssh/systemctl, no dispatch/sub-agent —
every one of those costs seconds-to-minutes; the page is rebuilt on a
fixed cadence so the answer is always one read away. If the page is
stale the correct action is surfacing the staleness, not recomputing.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from backend import report_meta

logger = logging.getLogger("ada_fleet_jobs")

# Contract-fixed read target — the producer upserts exactly this page.
_COLLECTION = "ada-cms-pages"
_PAGE_KEY = "fleet-jobs-report"
_PAGE_LANG = "en"
# Contract freshness window; meta.fresh_for carries it too, this is the
# fallback when the field is absent/unparseable.
_DEFAULT_FRESH_S = 4 * 3600

DECLARATION = {
    "name": "ada_fleet_jobs",
    "description": (
        "Fleet jobs/fleet status — 'how are jobs', 'job status', 'what "
        "is the fleet doing', timer failures, lanes, dispatch queue, "
        "24h spend. Reads the rendered fleet-jobs-report page; pass "
        "focus=spend|timers|lanes|queue|hosts for one section."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "focus": {
                "type": "string",
                "description": (
                    "Optional section to emphasize: overview (default), "
                    "spend, timers, lanes, queue, or hosts."
                ),
            },
        },
    },
}

_FOCUS_ALIASES = {
    "overview": "overview", "summary": "overview", "status": "overview",
    "job": "overview", "jobs": "overview", "fleet": "overview",
    "report": "overview", "everything": "overview",
    "spend": "spend", "cost": "spend", "litellm": "spend",
    "spending": "spend", "expense": "spend",
    "timer": "timers", "timers": "timers", "failing": "timers",
    "attention": "timers", "problems": "timers", "trouble": "timers",
    "host": "hosts", "hosts": "hosts", "hostname": "hosts",
    "lane": "lanes", "lanes": "lanes",
    "queue": "queue", "dispatch": "queue", "backlog": "queue",
    "claim": "queue", "claims": "queue", "working": "queue",
}

_MISSING_ANSWER = (
    "The fleet jobs report hasn't been generated yet — the "
    "fleet-jobs-report job (every 4h on idc03) hasn't published a page "
    "I can read. I can card it so someone checks the producer.")
_EMPTY_ANSWER = (
    "The fleet jobs report page exists but has no content yet — the "
    "producer may be mid-write.")


# -- small parsing helpers -------------------------------------------------


def _sections(md: str) -> list[tuple[str, list[str]]]:
    """Level-2 sections -> (title, body lines). '### ' stays in the body."""
    out: list[tuple[str, list[str]]] = []
    title: str | None = None
    buf: list[str] = []
    for line in md.splitlines():
        if line.startswith("## "):
            if title is not None:
                out.append((title, buf))
            title, buf = line[3:].strip(), []
        elif title is not None:
            buf.append(line)
    if title is not None:
        out.append((title, buf))
    return out


def _find_section(sections: list[tuple[str, list[str]]],
                  *needles: str) -> list[str]:
    for title, lines in sections:
        tl = title.lower()
        if any(n in tl for n in needles):
            return lines
    return []


def _subsection(lines: list[str], needle: str) -> list[str]:
    """Body lines after a '### <needle>' header (to the next ### or end)."""
    out: list[str] = []
    on = False
    for ln in lines:
        if ln.startswith("### "):
            on = needle in ln.lower()
            continue
        if on:
            out.append(ln)
    return out


def _sentence(text: str) -> str:
    t = " ".join(str(text or "").split()).strip()
    if not t:
        return ""
    if t[-1] not in ".!?":
        t += "."
    return t[0].upper() + t[1:]


def _term(text: str) -> str:
    """Period-terminated clause that must NOT be capitalized — host and
    timer names are case-significant identifiers."""
    t = " ".join(str(text or "").split()).strip()
    if t and t[-1] not in ".!?":
        t += "."
    return t


def _claim_totals(lines: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for ln in lines:
        m = re.match(r"\s*-\s*([A-Za-z][A-Za-z /]*?)\s*:\s*(\d+)\s*$", ln)
        if m:
            out[m.group(1).strip().lower()] = int(m.group(2))
    return out


def _queue_counts(md: str) -> dict[str, int | None]:
    """Claimable / in-flight / human-waiting — tolerant of the two label
    orders the producer has shipped ('Queued claimable' vs 'Claimable
    queued', 'Claimed/in-flight' vs 'In-flight claimed')."""
    def num(*pats: str) -> int | None:
        for p in pats:
            m = re.search(p, md, re.I)
            if m:
                return int(m.group(1))
        return None
    return {
        "claimable": num(r"claimable\s+queued\s*:\s*(\d+)",
                         r"queued\s+claimable\s*:\s*(\d+)"),
        "in_flight": num(r"in[- ]?flight\s+claimed\s*:\s*(\d+)",
                         r"claimed\s*/\s*in[- ]?flight\s*:\s*(\d+)",
                         r"claimed/in-flight\s*:\s*(\d+)"),
        "human": num(r"human[- ]?waiting\s*:\s*(\d+)"),
    }


def _queue_tables(md: str) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Runner heartbeat rows {host: {stale, active}} + queued card ids,
    from whichever queue table layout the page carries."""
    runners: dict[str, dict[str, str]] = {}
    cards: list[str] = []
    skip_heads = {"host", "runner", "pos", "lane", "key", "timer"}
    for ln in md.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 3 or all(set(c) <= set("-: ") for c in cells):
            continue
        c0 = cells[0].strip("` ")
        if not c0 or c0.lower() in skip_heads:
            continue
        # runner heartbeat row: | host | <age>s | yes|no | active |
        if (re.fullmatch(r"[\d.]+\s*s|\?s?", cells[1] or "")
                and (cells[2] or "").lower() in ("yes", "no")
                and not re.fullmatch(r"[\d.]+", c0)):
            runners[c0] = {
                "stale": cells[2].lower() == "yes",
                "active": cells[3] if len(cells) > 3 else "",
            }
            continue
        # queue position row: | <n> | `card-id` | lane | ...
        if cells[0].isdigit() and (cells[1] or "").startswith("`"):
            cards.append(cells[1].strip("`"))
    return runners, cards


def _host_trouble(sections: list[tuple[str, list[str]]]
                  ) -> list[dict[str, Any]]:
    """Per-host failing/late timer counts + names — tolerant of both page
    generations: '### Timer States - ok..failing..off' + bad-timer table
    (current) and 'Observed timers — emoji counts' + 'Attention timers'
    (2026-11-20 layout)."""
    hosts: list[dict[str, Any]] = []
    for title, lines in sections:
        if not title.lower().startswith("host:"):
            continue
        name = title.split(":", 1)[1].strip()
        failing = late = 0
        names: list[str] = []
        for ln in lines:
            s = ln.strip()
            # '- ok 23 · late 0 · failing 2 · off 3'
            if re.search(r"\bok\s+\d+", s, re.I):
                m = re.search(r"\bfailing\s+(\d+)\b", s, re.I)
                if m:
                    failing = max(failing, int(m.group(1)))
                m = re.search(r"\blate\s+(\d+)\b", s, re.I)
                if m:
                    late = max(late, int(m.group(1)))
            # 'Observed timers: 28 total — 🟢 23 · 🟡 0 · 🔴 2 · ⬛ 3'
            m = re.search(r"🔴\s*(\d+)", s)
            if m:
                failing = max(failing, int(m.group(1)))
            m = re.search(r"🟡\s*(\d+)", s)
            if m:
                late = max(late, int(m.group(1)))
            # '- Attention timers: `a` (failing), `b` (missing)'
            if s.lower().startswith("- attention timers:"):
                for t, state in re.findall(r"`([^`]+)`\s*\((\w+)\)", s):
                    if state.lower() in ("failing", "late") and t not in names:
                        names.append(t)
            # bad-timer table row: '| `timer` | failing | last | result |'
            bm = re.match(r"\|\s*`([^`]+)`\s*\|\s*(failing|late)\b",
                          s, re.I)
            if bm and bm.group(1) not in names:
                names.append(bm.group(1))
        hosts.append({"host": name, "failing": failing, "late": late,
                      "timers": names})
    return hosts


def _lane_rows(lane_lines: list[str]) -> list[dict[str, int | str]]:
    rows: list[dict[str, int | str]] = []
    for ln in _subsection(lane_lines, "action status by lane"):
        m = re.match(
            r"^\|\s*([A-Za-z][\w-]*)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|"
            r"\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|",
            ln.strip())
        if m:
            rows.append({"lane": m.group(1), "queued": int(m.group(2)),
                         "claimed": int(m.group(3)),
                         "running": int(m.group(4)),
                         "done": int(m.group(5)),
                         "failed": int(m.group(6))})
    return rows


def _spend_rows(lane_lines: list[str]) -> list[dict[str, Any]]:
    """'| key | model | calls | tokens | $spend |' rows under
    '### LiteLLM Spend (trailing 24h)'."""
    rows: list[dict[str, Any]] = []
    for ln in _subsection(lane_lines, "litellm spend"):
        m = re.match(
            r"\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|"
            r"\s*([\d,]+)\s*\|\s*\$?\s*([\d.]+)\s*\|",
            ln.strip())
        if m:
            rows.append({"key": m.group(1).strip(),
                         "model": m.group(2).strip(),
                         "calls": int(m.group(3)),
                         "tokens": int(m.group(4).replace(",", "")),
                         "usd": float(m.group(5))})
    return rows


def _active_summary(cell: str) -> str:
    c = (cell or "").strip()
    if not c or c in ("—", "-", "–"):
        return ""
    if re.fullmatch(r"\d+", c):
        return f"{c} active" if int(c) > 0 else ""
    names = [x.strip() for x in c.split(",") if x.strip()][:2]
    more = len([x for x in c.split(",") if x.strip()]) - 2
    return ", ".join(names) + (f" +{more} more" if more > 0 else "")


# -- answer builders ---------------------------------------------------------


def _queue_sentence(q: dict[str, int | None]) -> str:
    bits = []
    if q.get("claimable") is not None:
        bits.append(f"{q['claimable']} claimable")
    if q.get("in_flight") is not None:
        bits.append(f"{q['in_flight']} in-flight")
    if q.get("human") is not None:
        bits.append(f"{q['human']} waiting on humans")
    return "The live queue shows " + ", ".join(bits) if bits else ""


def _health_sentence(ctx: dict[str, Any]) -> str:
    hosts = ctx["hosts"]
    failing_hosts = [h for h in hosts if h["failing"] > 0]
    total_failing = sum(h["failing"] for h in hosts)
    late_total = sum(h["late"] for h in hosts)
    stale = sorted(h for h, r in ctx["runners"].items() if r["stale"])
    if total_failing:
        pairs = [f"{t} ({h['host']})"
                 for h in failing_hosts for t in h["timers"]]
        shown = ", ".join(pairs[:4])
        extra = len(pairs) - 4
        if extra > 0:
            shown += f" +{extra} more"
        s = (f"{total_failing} timer{'s' if total_failing != 1 else ''} "
             f"failing across {len(failing_hosts)} "
             f"host{'s' if len(failing_hosts) != 1 else ''}")
        if shown:
            s += f" — {shown}"
        return _sentence(s)
    if late_total:
        return _sentence(f"{late_total} timers running late")
    if stale:
        return _sentence("Runner heartbeats look stale on "
                         + ", ".join(stale[:4]))
    if not hosts and not ctx["runners"]:
        return _sentence("The report carries no per-host timer data")
    return _sentence("No failing timers on the observed hosts")


def _spend_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"rows": rows,
            "calls": sum(r["calls"] for r in rows),
            "usd": round(sum(r["usd"] for r in rows), 6)}


def _overview_body(ctx: dict[str, Any]) -> str:
    sents: list[str] = []
    if ctx["meta_summary"]:
        sents.append(_sentence(ctx["meta_summary"]))
    else:
        c = ctx["claims"]
        bits = [f"{c[k]} {label}" for k, label in (
            ("queued", "queued"), ("claimed", "claimed"),
            ("running", "running"), ("done", "done"),
            ("failed", "failed")) if c.get(k)]
        if bits:
            sents.append(_sentence("Board claims: " + ", ".join(bits)))
        qs = _queue_sentence(ctx["queue"])
        if qs:
            sents.append(_sentence(qs))
    health = _health_sentence(ctx)
    if health:
        sents.append(health)
    sp = ctx["spend"]
    if sp["usd"] > 0:
        sents.append(_sentence(
            f"LiteLLM logged ${sp['usd']:.2f} across {sp['calls']} "
            "calls in the last 24 hours"))
    return " ".join(sents[:4])


def _spend_body(ctx: dict[str, Any]) -> str:
    sp = ctx["spend"]
    if not sp["rows"]:
        return _sentence(
            "The report shows no LiteLLM spend data for the trailing 24 "
            "hours — the ledger was empty or unreachable")
    s = (f"LiteLLM spend in the trailing 24 hours: ${sp['usd']:.2f} "
         f"across {sp['calls']} calls")
    top = max(sp["rows"], key=lambda r: r["usd"])
    if top["usd"] > 0:
        s += f"; top entry {top['model'] or top['key']} at ${top['usd']:.2f}"
    return _sentence(s)


def _timers_body(ctx: dict[str, Any]) -> str:
    parts: list[str] = []
    for h in ctx["hosts"]:
        bits = []
        if h["failing"]:
            bits.append(f"{h['failing']} failing")
        if h["late"]:
            bits.append(f"{h['late']} late")
        if not bits:
            continue
        s = f"{h['host']}: " + " · ".join(bits)
        if h["timers"]:
            s += " (" + ", ".join(h["timers"][:3]) + ")"
        parts.append(_term(s))
    if not parts:
        return _sentence("No failing or late timers in the report")
    return " ".join(parts[:4])


def _lanes_body(ctx: dict[str, Any]) -> str:
    rows = [r for r in ctx["lanes"]
            if r["queued"] or r["claimed"] or r["running"]]
    if not rows:
        return _sentence("No lane activity in the report")
    rows.sort(key=lambda r: (-int(r["running"]), -int(r["queued"]),
                             str(r["lane"])))
    parts = []
    for r in rows[:4]:
        bits = []
        if r["running"]:
            bits.append(f"{r['running']} running")
        if r["queued"]:
            bits.append(f"{r['queued']} queued")
        if r["claimed"]:
            bits.append(f"{r['claimed']} claimed")
        parts.append(f"{r['lane']} ({', '.join(bits)})")
    return _sentence("Lanes: " + "; ".join(parts))


def _queue_body(ctx: dict[str, Any]) -> str:
    sents: list[str] = []
    qs = _queue_sentence(ctx["queue"])
    if qs:
        sents.append(_sentence(qs))
    if ctx["cards"]:
        sents.append(_sentence("Next up: " + ", ".join(ctx["cards"][:5])))
    if not sents:
        sents.append(_sentence(
            "The report doesn't carry a live queue snapshot"))
    return " ".join(sents[:3])


def _hosts_body(ctx: dict[str, Any]) -> str:
    parts: list[str] = []
    for h in ctx["hosts"]:
        bits = []
        if h["failing"]:
            bits.append(f"{h['failing']} failing timers")
        if h["late"]:
            bits.append(f"{h['late']} late")
        if bits:
            s = f"{h['host']} ({', '.join(bits)})"
            if h["timers"]:
                s += ": " + ", ".join(h["timers"][:3])
            parts.append(s)
    for host, r in sorted(ctx["runners"].items()):
        if r["stale"]:
            parts.append(f"{host} (stale runner heartbeat)")
    work = [f"{h} ({_active_summary(r['active'])})"
            for h, r in sorted(ctx["runners"].items())
            if _active_summary(r["active"])]
    if not parts and not work:
        return _sentence("No hosts need attention in this report")
    s = ""
    if parts:
        s = "Hosts needing attention: " + "; ".join(parts[:5])
    if work:
        s += (". " if s else "") + "Working now: " + ", ".join(work[:5])
    return _sentence(s)


def _body(focus: str, ctx: dict[str, Any]) -> str:
    if focus == "spend":
        return _spend_body(ctx)
    if focus == "timers":
        return _timers_body(ctx)
    if focus == "lanes":
        return _lanes_body(ctx)
    if focus == "queue":
        return _queue_body(ctx)
    if focus == "hosts":
        return _hosts_body(ctx)
    return _overview_body(ctx)


# -- freshness gate ----------------------------------------------------------


def _parse_ts(raw: Any) -> datetime | None:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _freshness(body: str, meta: dict[str, Any],
               now: datetime) -> tuple[str, dict[str, Any]]:
    """Contract gate: meta.updated vs meta.fresh_for (4h). Returns
    (answer, freshness-info)."""
    updated_raw = report_meta.first(meta, "updated")
    ff_raw = report_meta.first(meta, "fresh_for")
    secs = report_meta.fresh_for_seconds(ff_raw) or _DEFAULT_FRESH_S
    info: dict[str, Any] = {
        "state": "unknown", "updated": updated_raw or None,
        "fresh_for": ff_raw or None, "fresh_for_s": secs, "age_s": None,
    }
    dt = _parse_ts(updated_raw)
    if dt is None:
        return (_term(body) + " I couldn't read the report's "
                "timestamp, so treat this as unverified.", info)
    age = (now - dt).total_seconds()
    info["age_s"] = int(age)
    if age <= secs:
        info["state"] = "fresh"
        return _term(body), info
    info["state"] = "stale"
    stamp = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fh = ff_raw or "4h"
    return (_sentence(f"As of {stamp}, {_term(body)} The report is "
                      f"older than its {fh} freshness window — the "
                      "fleet-jobs-report producer may be down."), info)


# -- entry point -------------------------------------------------------------


def _mddb(runner: Any) -> Any | None:
    """The routed mddb client via the runner.context facade (the ONE
    seam), falling back to runner.mddb for bare test fakes."""
    ctx = getattr(runner, "context", None)
    m = getattr(ctx, "mddb", None) if ctx is not None else None
    return m if m is not None else getattr(runner, "mddb", None)


def _focus(arg: Any) -> str:
    raw = " ".join(str(arg or "").lower().split())
    if not raw:
        return "overview"
    key = raw.replace(" ", "")
    if key in _FOCUS_ALIASES:
        return _FOCUS_ALIASES[key]
    for w in raw.split():
        if w in _FOCUS_ALIASES:
            return _FOCUS_ALIASES[w]
    return "overview"


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    focus = _focus(args.get("focus"))
    mddb = _mddb(runner)
    if mddb is None:
        return {"ok": False,
                "error": "my memory store isn't available in this mode"}
    try:
        doc = await mddb.get_document(_COLLECTION, _PAGE_KEY, _PAGE_LANG)
    except Exception as exc:  # contract: unreachable == not generated
        logger.warning("ada_fleet_jobs get_document failed: %s", exc)
        doc = None
    if not isinstance(doc, dict):
        return {"ok": True, "generated": False, "mode": focus,
                "answer": _MISSING_ANSWER,
                "freshness": {"state": "missing"}}
    md = str(doc.get("contentMd") or "")
    meta = doc.get("meta") or {}
    if not md.strip():
        return {"ok": True, "generated": False, "mode": focus,
                "answer": _EMPTY_ANSWER,
                "freshness": {"state": "unknown"}}
    try:
        sections = _sections(md)
        lane_lines = _find_section(sections, "lanes")
        runners, cards = _queue_tables(md)
        spend_rows = _spend_rows(lane_lines)
        ctx: dict[str, Any] = {
            "meta_summary": report_meta.first(meta, "summary"),
            "claims": _claim_totals(
                _find_section(sections, "claim totals")),
            "queue": _queue_counts(md),
            "runners": runners,
            "cards": cards,
            "hosts": _host_trouble(sections),
            "lanes": _lane_rows(lane_lines),
            "spend": _spend_totals(spend_rows),
        }
        body = _body(focus, ctx)
        answer, fresh_info = _freshness(body, meta,
                                        datetime.now(timezone.utc))
        stats = {k: v for k, v in {
            "queued": ctx["claims"].get("queued"),
            "running": ctx["claims"].get("running"),
            "failed": ctx["claims"].get("failed"),
            "claimable": ctx["queue"].get("claimable"),
            "in_flight": ctx["queue"].get("in_flight"),
            "failing_timers": sum(h["failing"] for h in ctx["hosts"]),
            "spend_usd": ctx["spend"]["usd"] or None,
        }.items() if v is not None}
        return {
            "ok": True,
            "mode": focus,
            "answer": answer,
            "freshness": fresh_info,
            "report": {
                "collection": _COLLECTION,
                "key": _PAGE_KEY,
                "title": (report_meta.first(meta, "title")
                          or _title(md)),
                "summary": ctx["meta_summary"] or None,
            },
            "stats": stats,
        }
    except Exception as exc:  # never raise into the model
        logger.warning("ada_fleet_jobs answer build failed: %s", exc)
        return {"ok": False,
                "error": "couldn't read the fleet jobs report"}


def _title(md: str) -> str:
    for line in md.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""
