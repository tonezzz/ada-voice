"""Per-tool daily spend caps + budget readout (card
ada-spend-guardrails-per-tool-daily-caps).

Two enforcement surfaces share one ledger scan:

1. Runner gate — ToolRunner._execute_gated calls check(name) before a
   tool executes. Covers flat-cost tools whose spend never crosses a
   token API (ada_render_video meters est_cost_usd at Veo submit).
2. Aux-call gate — gemini_pool.generate calls check_key(tool) before
   each generate_content call. Covers every token-metered side call:
   web_search, cms_edit, doc_read, render_image/check, session
   summaries, rollups, deep research — they all carry a tool= label.

Spend = sum of usage_tracker.entry_cost_usd over ADA_USAGE_LOG rows for
the UTC day whose usage key matches. A row's key is rec["tool"] (the
label generate() callers pass; usage_tracker.record gained the field)
falling back to rec["source"] for older rows ("live", "render_video",
"posture", ...). Day boundary is UTC — the ledger's own by_day bucketing
uses the same ts[:10] slice, so caps and the usage report agree.

Caps are opt-in per key, first match wins:
  ADA_TOOL_DAILY_CAP_<KEY>   env, KEY uppercased non-alnum->_
                             (e.g. ADA_TOOL_DAILY_CAP_ADA_RENDER_VIDEO);
                             the canonical tool's env wins for alias
                             labels too — env always overrides manifest
  manifest daily_cap_usd     tools.d manifest entry for the canonical
                             tool name
  ADA_TOOL_DAILY_CAP_DEFAULT_USD   catch-all for uncapped keys
0/unset everywhere = uncapped. Alias labels resolve to their canonical
tool's cap AND spend set (generate() sees "render_image", the cap is
declared on "ada_render_image").

status() feeds the ada_ops action='budget' readout and
conversation_health — remaining daily budgets stay visible instead of
silent.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("tool_budget")


class ToolBudgetExceeded(RuntimeError):
    """A tool/aux call would push its key past the configured daily cap.
    Carries .key/.spent/.cap for callers that want structured detail."""

    def __init__(self, key: str, spent: float, cap: float) -> None:
        self.key = key
        self.spent = spent
        self.cap = cap
        super().__init__(
            f"daily spend cap reached for '{key}' — est ${spent:.2f} of "
            f"${cap:.2f} today (UTC); resets at midnight UTC or raise "
            f"ADA_TOOL_DAILY_CAP_{_env_key(key)}")


def _env_key(key: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(key).upper()).strip("_")


def _env_float(name: str, default: float = 0.0) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def day() -> str:
    """UTC day key — matches usage_tracker's by_day ts[:10] bucketing."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _usage_log_path() -> Path | None:
    raw = os.environ.get("ADA_USAGE_LOG", "data/usage.jsonl")
    if str(raw).strip().lower() in ("", "off", "none"):
        return None
    return Path(str(raw))


# ------------------------------------------------------- key aliasing

# Canonical tool name -> extra usage-ledger keys whose spend bills to it.
# generate() callers label calls with short names; the cap is declared on
# the canonical tool. Sources that aren't tools at all (posture/clutter
# verifiers, the Live session itself) are deliberately absent — they get
# their own cap keys via env, enforced at their own call sites or not at
# all (lane_budget owns the Live lane's monthly ceiling).
_SPEND_KEYS: dict[str, frozenset[str]] = {
    "ada_render_video": frozenset({"render_video"}),
    "ada_render_image": frozenset({"render_image", "render_check"}),
    "ada_ops": frozenset({
        "summary", "session_summary", "session_report",
        "extract_candidates", "extract_actions", "rollup"}),
    "web_search": frozenset({"web_search"}),
    "doc_read": frozenset({"doc_read"}),
    "cms_edit": frozenset({"cms_edit"}),
    "cms_publish_page": frozenset({"cms_publish_page"}),
    "cms_delete_page": frozenset({"cms_delete_page"}),
    "cms_note_update": frozenset({"cms_note_update"}),
}

_LABEL_TO_CANONICAL: dict[str, str] = {
    label: canon
    for canon, labels in _SPEND_KEYS.items() for label in labels
}


def spend_keys_for(key: str) -> frozenset[str]:
    """Every usage-ledger key whose spend counts against `key`'s cap.
    A canonical name pulls in its alias labels; an alias label pulls in
    its canonical's whole set (the cap is per tool, not per call site)."""
    key = str(key or "").strip()
    if not key:
        return frozenset()
    canon = key if key in _SPEND_KEYS else _LABEL_TO_CANONICAL.get(key)
    if canon is None:
        return frozenset({key})
    return frozenset({canon}) | _SPEND_KEYS[canon] | ({key} if key != canon else frozenset())


def _canonical_for(key: str) -> str:
    key = str(key or "").strip()
    return key if key in _SPEND_KEYS else _LABEL_TO_CANONICAL.get(key, key)


# ------------------------------------------------------------- config


def _manifest_cap(canonical: str) -> float:
    """tools.d manifest daily_cap_usd for the canonical tool — 0 when the
    tool isn't dynamic or declares no cap. Registry load failures are
    non-fatal (env caps still apply)."""
    try:
        from backend import tools_loader
        spec = tools_loader.registry().tools.get(canonical)
        return float(getattr(spec, "daily_cap_usd", 0.0) or 0.0)
    except Exception:
        return 0.0


def cap_usd(key: str) -> float:
    """Configured daily cap for a usage key/canonical tool; <=0 = uncapped.
    Order: per-key env > per-canonical env > manifest > global default."""
    key = str(key or "").strip()
    if not key:
        return 0.0
    canon = _canonical_for(key)
    env_key = _env_float(f"ADA_TOOL_DAILY_CAP_{_env_key(key)}", -1.0)
    if env_key >= 0:
        return env_key
    if canon != key:
        env_canon = _env_float(
            f"ADA_TOOL_DAILY_CAP_{_env_key(canon)}", -1.0)
        if env_canon >= 0:
            return env_canon
    manifest = _manifest_cap(canon)
    if manifest > 0:
        return manifest
    return _env_float("ADA_TOOL_DAILY_CAP_DEFAULT_USD", 0.0)


# --------------------------------------------------------- spend scan

_scan_cache: dict[str, Any] = {"key": None, "day": "", "by_key": {}}


def _scan(day_key: str) -> dict[str, float]:
    """{usage_key: est_usd} for one UTC day, cached on file stat —
    retry/gate loops don't re-scan the ledger."""
    path = _usage_log_path()
    if path is None:
        return {}
    try:
        st = path.stat()
    except OSError:
        return {}
    ck = (str(path), st.st_size, st.st_mtime_ns, day_key)
    if _scan_cache["key"] == ck:
        return dict(_scan_cache["by_key"])
    from backend.usage_tracker import entry_cost_usd
    by_key: dict[str, float] = {}
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(rec.get("ts") or "")[:10] != day_key:
                    continue
                k = str(rec.get("tool") or rec.get("source") or "unknown")
                # entry_cost_usd is token-derived only — the flat 'cost'
                # field (Veo per-render estimates etc.) bills on top,
                # same as usage_tracker._report's est_cost_usd.
                by_key[k] = by_key.get(k, 0.0) + entry_cost_usd(rec) \
                    + float(rec.get("cost") or 0)
    except OSError:
        return {}
    by_key = {k: round(v, 4) for k, v in by_key.items()}
    _scan_cache.update(key=ck, day=day_key, by_key=by_key)
    return dict(by_key)


def day_spend_usd(key: str, day_key: str | None = None) -> float:
    """Estimated USD `key` (tool or usage label) has spent today."""
    day_key = day_key or day()
    by_key = _scan(day_key)
    return round(sum(by_key.get(k, 0.0) for k in spend_keys_for(key)), 4)


# ------------------------------------------------------------ gates


def exceeded(key: str, day_key: str | None = None
             ) -> tuple[float, float] | None:
    """(spent, cap) when `key` is over its daily cap today, else None."""
    cap = cap_usd(key)
    if cap <= 0:
        return None
    spent = day_spend_usd(key, day_key)
    if spent >= cap:
        return spent, cap
    return None


def check(name: str) -> dict[str, Any] | None:
    """Runner-gate denial dict for an over-cap tool, else None. Returned
    (not raised) so _execute_gated can short-circuit like the storm
    breaker — the model gets an actionable error, not an exception."""
    hit = exceeded(name)
    if hit is None:
        return None
    spent, cap = hit
    keys = sorted(spend_keys_for(name))
    return {
        "ok": False,
        "error": (
            f"{name} is over its daily spend cap — est ${spent:.2f} of "
            f"${cap:.2f} today (UTC, keys: {', '.join(keys)}). It resets "
            "at midnight UTC; do not retry this tool today — tell the "
            "user plainly and offer an alternative."),
        "error_type": "ToolBudgetExceeded",
        "budget": {
            "tool": name,
            "day": day(),
            "spent_usd": spent,
            "cap_usd": cap,
            "keys": keys,
        },
    }


def check_key(key: str) -> None:
    """Raise ToolBudgetExceeded when an aux-call label is over cap —
    gemini_pool.generate's pre-flight gate."""
    hit = exceeded(key)
    if hit is not None:
        raise ToolBudgetExceeded(key, hit[0], hit[1])


# ------------------------------------------------------------- status


def status(day_key: str | None = None) -> dict[str, Any]:
    """Budget readout — every usage key with spend today plus every
    configured cap, with remaining budget. Keys are usage labels
    (render_image, web_search, summary...) resolved to their canonical
    tool where one exists."""
    day_key = day_key or day()
    by_key = _scan(day_key)
    spend: dict[str, float] = {}
    for k, usd in by_key.items():
        canon = _canonical_for(k)
        spend[canon] = round(spend.get(canon, 0.0) + usd, 4)
    caps: dict[str, dict[str, Any]] = {}
    for canon in sorted(set(spend) | set(_SPEND_KEYS)):
        cap = cap_usd(canon)
        if cap <= 0:
            continue
        spent = spend.get(canon, 0.0)
        caps[canon] = {
            "cap_usd": cap,
            "spent_usd": spent,
            "remaining_usd": round(cap - spent, 4),
            "keys": sorted(spend_keys_for(canon)),
        }
    return {
        "day": day_key,
        "day_boundary": "UTC midnight",
        "default_cap_usd": _env_float("ADA_TOOL_DAILY_CAP_DEFAULT_USD", 0.0)
                           or None,
        "usage_log": str(_usage_log_path() or "off"),
        "spend_by_tool": dict(sorted(spend.items())),
        "capped": caps,
        "note": ("caps are per-tool daily USD estimates from the usage "
                 "ledger; uncapped tools appear in spend_by_tool only. "
                 "Configure via ADA_TOOL_DAILY_CAP_<TOOL>, manifest "
                 "daily_cap_usd, or ADA_TOOL_DAILY_CAP_DEFAULT_USD."),
    }


def reset() -> None:
    """Test hook — drop the spend scan cache."""
    _scan_cache.update(key=None, day="", by_key={})
