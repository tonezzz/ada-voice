#!/usr/bin/env python3
"""Read-only verification of ada_mcp board read path (dispatch card ada-ocb-kanban-smoke)."""
import json
import sys
import time

sys.path.insert(0, "opencode")
import ada_mcp as m  # noqa: E402

THIS_CARD = "ada-ocb-kanban-smoke"
results = []


def timed(step, fn):
    m._snapshot_cache.clear()  # measure real fetch path, not the 20s cache
    t0 = time.monotonic()
    try:
        out = fn()
        err = None
    except Exception as exc:  # noqa: BLE001
        out, err = None, f"{type(exc).__name__}: {exc}"
    dt = time.monotonic() - t0
    rec = {"step": step, "elapsed_s": round(dt, 3), "ok": err is None, "error": err}
    results.append(rec)
    return out, rec


def mutations_nonempty(obj, path=""):
    """Yield paths of any non-empty mutations/mutations_performed fields."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if k in ("mutations", "mutations_performed") and v not in (None, [], ""):
                hits.append((p, v))
            hits += mutations_nonempty(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += mutations_nonempty(v, f"{path}[{i}]")
    return hits


# 1. find_card on this card + ADA-0172
for ref in (THIS_CARD, "ADA-0172"):
    card, rec = timed(f"find_card({ref})", lambda r=ref: m.find_card(r))
    if rec["ok"]:
        rec["resolved"] = {"id": card.get("id"), "ref": card.get("ref"),
                           "title": card.get("title"), "column": card.get("column")}
        rec["under_10s"] = rec["elapsed_s"] < 10
        rec["mutations"] = mutations_nonempty(card)

# 2. _board_cards() and _compact_cards() row counts
for name, fn in (("_board_cards()", m._board_cards), ("_compact_cards()", m._compact_cards)):
    rows, rec = timed(name, fn)
    if rec["ok"]:
        rec["rows"] = len(rows)

# 3. _pick_cards raw + ada_card_pick (token-issuing public tool) for sweep and queue
for strategy in ("sweep", "queue"):
    picks, rec = timed(f"_pick_cards({strategy},limit=3)",
                       lambda s=strategy: m._pick_cards(s, 7, 3))
    if rec["ok"]:
        rec["picks"] = len(picks)
        rec["mutations"] = mutations_nonempty(picks)
    raw, rec2 = timed(f"ada_card_pick({strategy},limit=3)",
                      lambda s=strategy: m.ada_card_pick(strategy=s, limit=3))
    if rec2["ok"]:
        data = json.loads(raw)
        rec2["picks"] = len(data.get("picks", []))
        rec2["tokens"] = all("selection_token" in p for p in data.get("picks", []))
        rec2["mutations_performed"] = data.get("mutations_performed")
        rec2["mutations"] = mutations_nonempty(data)

# 4. _context_card on this card ref
card, rec = timed(f"_context_card({THIS_CARD})",
                  lambda: m._context_card(THIS_CARD, time.monotonic() + 15))
if rec["ok"]:
    want = ("id", "ref", "title", "column", "priority", "updated",
            "brief", "spec", "action", "requests", "comms")
    rec["fields_present"] = [k for k in want if k in card]
    rec["fields_missing"] = [k for k in want if k not in card]
    rec["comms_len"] = len(card.get("comms") or [])
    rec["mutations"] = mutations_nonempty(card)

# 5. aggregate mutations verdict
bad = [r["step"] for r in results if r.get("mutations")]
verdict = {"all_ok": all(r["ok"] for r in results),
           "nonempty_mutations": bad or "none"}
print(json.dumps({"results": results, "verdict": verdict}, indent=2, default=str))
