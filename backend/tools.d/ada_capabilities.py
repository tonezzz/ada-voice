"""ada_capabilities — "what can you do" answered from the generated snapshot.

Consumer half of chaba's docs/kb/ada-tools-verb-contract.md (card
ada-capability-verb-wiring). The producer is chaba's
scripts/ada/tools-commands-catalog.py, which parses every declaration
source and emits docs/kb/ada-tools-commands.json. This module NEVER parses
declarations live — it reads the vendored machine snapshot once per file
mtime and fills a fixed template, so a capability ask costs one cached
file read (<50 ms) with no model calls, no AST, no network.

Snapshot resolution order:
  1. backend/data/ada-tools-snapshot.json — deploy-refreshed runtime copy
     (gitignored; deploy-ada.sh refreshes it from the chaba checkout)
  2. backend/generated/ada-tools-commands.json — vendored copy that ships
     with the code it describes

Answers:
  no topic  — grouped capability summary (deterministic group order,
              canonical tool names only; absorbed alias names are never
              presented as capabilities)
  topic     — detail mode: exact tool name, retired/absorbed name
              (merged_into honesty), family, or keyword match

Failure contract: missing/corrupt snapshot or a schema major bump ->
ok:false, never a fabricated list. Stale provenance (source.behind>0 or
generated_at >30d) still answers, with an "as of" caveat + drift log.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("ada_capabilities")

DECLARATION = {
    "name": "ada_capabilities",
    "description": (
        "What Ada can do — capability list, tools and triggers, answered "
        "from the generated catalog snapshot. Use for 'what can you do', "
        "'can you X', 'how does tool Y work'. Read-only; instant."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": (
                    "Optional — a tool name, family name, or keyword to "
                    "describe in detail instead of listing every "
                    "capability."
                ),
            },
        },
    },
}

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_SNAPSHOT_CANDIDATES = (
    _BACKEND_DIR / "data" / "ada-tools-snapshot.json",
    _BACKEND_DIR / "generated" / "ada-tools-commands.json",
)
_SCHEMA_MAJOR = 1
_STALE_S = 30 * 24 * 3600
_MAX_DETAIL_MATCHES = 6
_MAX_TRIGGER_EXAMPLES = 3

# Ordered (label, name-substrings) groups — first match wins, unmatched
# tools land in "other". Deterministic: same snapshot -> same groups.
_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("memory & voice", (
        "memory", "remember", "recall", "persona", "enroll", "speaker",
        "facial", "forget")),
    ("calendar, tasks & plans", (
        "calendar", "tasks", "plan_day", "trip")),
    ("home, sensors & weather", (
        "home", "control_entity", "ha_", "weather", "rk600",
        "inverter", "battery", "power", "pool")),
    ("cameras, screens & displays", (
        "camera", "cctv", "cast", "vcast", "snapshot", "tv_", "yt",
        "gev", "look", "gesture")),
    ("kanban, jobs & hosts", (
        "kanban", "devin", "nest_status", "sweep", "opencode",
        "host_status", "track_device")),
    ("docs, wiki & boards", (
        "cms", "docs", "drive", "doc_", "wiki", "wp", "cpt", "lesson",
        "journal")),
    ("news, reports & data", (
        "news", "report", "market", "quote", "place")),
    ("media & rendering", (
        "render", "percussion", "capture_eval", "voice_fx", "adobe",
        "stock", "gs_")),
)
_OTHER_LABEL = "other tools"

# module-level cache keyed on (path, mtime) — one read per file version.
_cache: dict[str, Any] = {"key": None, "data": None, "error": None}


def _snapshot_path() -> Path | None:
    override = os.environ.get("ADA_CAPABILITIES_SNAPSHOT")
    if override:
        p = Path(override)
        return p if p.is_file() else None
    for p in _SNAPSHOT_CANDIDATES:
        if p.is_file():
            return p
    return None


def _load() -> tuple[dict[str, Any] | None, str | None]:
    """Return (snapshot, error). Reloads only when the file changed."""
    path = _snapshot_path()
    if path is None:
        return None, ("capability list not generated — run the catalog "
                      "producer (chaba scripts/ada/tools-commands-catalog.py)")
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None, "capability snapshot unreadable"
    key = f"{path}:{mtime}"
    if _cache["key"] == key:
        return _cache["data"], _cache["error"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(
                data.get("tools"), list):
            raise ValueError("unexpected shape")
    except Exception as exc:
        _cache.update(key=key, data=None,
                      error=f"capability snapshot corrupt ({exc}) — "
                            "regenerate it")
        return None, _cache["error"]
    _cache.update(key=key, data=data, error=None)
    return data, None


def _canonical_tools(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """name -> live tool row. Absorbed/alias names are back-compat routing,
    never surface — the producer still emits rows for tools.d implementation
    seats (e.g. wp_post.py behind manifest `wp`), so alias keys are
    filtered out here. Guest rows share canonical names; first row wins."""
    aliases = _aliases(data)
    out: dict[str, dict[str, Any]] = {}
    for t in data.get("tools") or []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        name = str(t["name"])
        if name in aliases:
            continue
        out.setdefault(name, t)
    return out


def _aliases(data: dict[str, Any]) -> dict[str, str]:
    return {str(k): str(v)
            for k, v in (data.get("aliases") or {}).items()}


def _absorbed_family(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """absorbed name -> family row (merged_into honesty)."""
    out: dict[str, dict[str, Any]] = {}
    for fam, row in (data.get("families") or {}).items():
        if not isinstance(row, dict):
            continue
        for absorbed in row.get("absorbed") or []:
            out[str(absorbed)] = {"family": str(fam), **row}
    return out


def _group(name: str) -> str:
    low = name.lower()
    for label, needles in _GROUPS:
        if any(n in low for n in needles):
            return label
    return _OTHER_LABEL


def _is_stale(data: dict[str, Any]) -> bool:
    src = data.get("source") or {}
    try:
        if int(src.get("behind") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    gen = str(data.get("generated_at") or "")
    try:
        ts = datetime.fromisoformat(gen.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() > _STALE_S
    except ValueError:
        return False


def _as_of(data: dict[str, Any]) -> str:
    src = data.get("source") or {}
    return str(src.get("head_date") or data.get("generated_at") or "?")[:10]


def _summary(data: dict[str, Any]) -> dict[str, Any]:
    tools = _canonical_tools(data)
    groups: dict[str, list[str]] = {}
    for name in sorted(tools):
        groups.setdefault(_group(name), []).append(name)
    core = sum(1 for t in tools.values() if t.get("layer") == "builtin")
    addon = len(tools) - core

    lines = [
        f"I have {len(tools)} capabilities — {core} core + {addon} "
        f"add-on tools — as of {_as_of(data)}.",
    ]
    for label, _ in _GROUPS:
        names = groups.get(label)
        if names:
            lines.append(f"- {label} ({len(names)}): "
                         + ", ".join(names) + ".")
    others = groups.get(_OTHER_LABEL)
    if others:
        lines.append(f"- {_OTHER_LABEL} ({len(others)}): "
                     + ", ".join(others) + ".")
    lines.append("Ask me about a specific tool or area for details.")
    if _is_stale(data):
        lines.append(f"Note: this list is as of {_as_of(data)} and may "
                     "be behind the latest tool changes.")
        logger.warning("ada_capabilities snapshot is stale "
                       "(as of %s)", _as_of(data))
    return {
        "ok": True,
        "answer": "\n".join(lines),
        "as_of": _as_of(data),
        "capability_count": len(tools),
        "groups": {k: v for k, v in groups.items()},
    }


def _tool_line(name: str, t: dict[str, Any],
               absorbed: dict[str, dict[str, Any]]) -> str:
    purpose = str(t.get("purpose") or "").strip()
    bits = [f"{name}: {purpose}" if purpose else name]
    trig = [str(x) for x in (t.get("triggers") or [])
            if str(x).strip()][: _MAX_TRIGGER_EXAMPLES]
    if trig:
        bits.append("triggers like " + "; ".join(f'"{x}"' for x in trig))
    fam = absorbed.get(name)
    if fam:
        bits.append(f"family {fam['family']} ({fam.get('status')})")
    elif t.get("family"):
        bits.append(f"family {t['family']}")
    policy = t.get("policy")
    if policy:
        bits.append(f"policy {policy}")
    return " — ".join(bits)


def _detail(data: dict[str, Any], topic: str) -> dict[str, Any]:
    tools = _canonical_tools(data)
    aliases = _aliases(data)
    absorbed = _absorbed_family(data)
    families = data.get("families") or {}
    q = topic.strip().lower()

    # 1. exact canonical tool name
    hit = tools.get(q) or tools.get(topic.strip())
    if hit is not None:
        name = str(hit["name"])
        return {"ok": True, "topic": topic,
                "answer": _tool_line(name, hit, absorbed),
                "tool": {"name": name, "purpose": hit.get("purpose"),
                         "policy": hit.get("policy"),
                         "family": hit.get("family") or None,
                         "triggers": hit.get("triggers") or []}}

    # 2. retired/absorbed name — merged_into honesty, never listed as a
    #    capability itself
    canonical = aliases.get(q) or aliases.get(topic.strip())
    fam = absorbed.get(q) or absorbed.get(topic.strip())
    if canonical or fam:
        target = canonical or (fam or {}).get("canonical")
        fam_name = (fam or {}).get("family") or ""
        where = f"family {fam_name}" if fam_name else "the tool surface"
        if target and target in tools:
            detail = _tool_line(str(target), tools[target], absorbed)
            return {"ok": True, "topic": topic,
                    "answer": (f"{topic.strip()} is retired — merged "
                               f"into {target} ({where}). {detail}"),
                    "merged_into": target}
        return {"ok": True, "topic": topic,
                "answer": (f"{topic.strip()} is retired — merged into "
                           f"{target or 'a newer tool'} ({where}).")}

    # 3. family name
    fam_row = families.get(q) or families.get(topic.strip())
    if isinstance(fam_row, dict):
        canon = str(fam_row.get("canonical") or "")
        absorbed_names = [str(a) for a in fam_row.get("absorbed") or []]
        members = sorted(n for n in tools
                         if str(tools[n].get("family") or "") == q
                         or n == canon)
        lines = [f"Family {q} ({fam_row.get('status') or 'active'}):"]
        for n in members[:_MAX_DETAIL_MATCHES]:
            lines.append("- " + _tool_line(n, tools[n], absorbed))
        for a in absorbed_names[:_MAX_DETAIL_MATCHES]:
            lines.append(f"- {a} — retired, merged into {canon}")
        if fam_row.get("merge_card"):
            lines.append(f"merge card: {fam_row['merge_card']}")
        return {"ok": True, "topic": topic,
                "answer": "\n".join(lines),
                "family": {"name": q, "canonical": canon,
                           "absorbed": absorbed_names,
                           "status": fam_row.get("status")}}

    # 4. keyword match over name + purpose + triggers
    matches = []
    for name in sorted(tools):
        t = tools[name]
        hay = " ".join([name, str(t.get("purpose") or ""),
                        " ".join(str(x) for x in
                                 (t.get("triggers") or []))]).lower()
        if q in hay:
            matches.append(_tool_line(name, t, absorbed))
            if len(matches) >= _MAX_DETAIL_MATCHES:
                break
    if matches:
        return {"ok": True, "topic": topic,
                "answer": (f"Tools matching \"{topic.strip()}\":\n"
                           + "\n".join("- " + m for m in matches))}

    return {"ok": True, "topic": topic,
            "answer": (f"Nothing in the capability catalog matches "
                       f"\"{topic.strip()}\" — ask about a tool name, a "
                       "family, or an area like memory, home, or kanban.")}


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    data, err = _load()
    if data is None:
        return {"ok": False, "error": err}
    try:
        major = int(data.get("schema") or 0)
    except (TypeError, ValueError):
        major = 0
    if major != _SCHEMA_MAJOR:
        return {"ok": False,
                "error": (f"capability snapshot schema v{major} is not "
                          f"supported (this consumer reads v{_SCHEMA_MAJOR}) "
                          "— regenerate the producer/consumer pair")}
    topic = str(args.get("topic") or "").strip()
    if topic:
        out = _detail(data, topic)
    else:
        out = _summary(data)
    out["as_of"] = _as_of(data)
    return out
