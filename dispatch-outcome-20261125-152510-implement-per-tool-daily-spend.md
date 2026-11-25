# Dispatch outcome — per-tool daily spend caps + budget readout

Card: `ada-spend-guardrails-per-tool-daily-caps-a376c3`
Branch: `dispatch/20261125-152510-implement-per-tool-daily-spend` (worktree only — not pushed, per dispatch rules)

## What changed

**New: `backend/tool_budget.py`** — per-tool daily USD spend caps and the
budget readout, built on the existing usage ledger (`ADA_USAGE_LOG`).

- Day boundary is UTC (same `ts[:10]` slice as the ledger's `by_day`).
- Spend key = `rec["tool"]` falling back to `rec["source"]` for older rows;
  spend = `entry_cost_usd(rec)` (token-derived) **plus** the flat `cost`
  field (Veo per-render estimates) — matching `_report`'s est_cost_usd.
- Cap resolution: `ADA_TOOL_DAILY_CAP_<KEY>` env (canonical env covers
  alias labels) > tools.d manifest `daily_cap_usd` >
  `ADA_TOOL_DAILY_CAP_DEFAULT_USD` catch-all. 0/unset = uncapped.
- `_SPEND_KEYS` alias map resolves `generate()` labels to canonical tools
  both ways (e.g. cap on `ada_render_image` covers `render_image` +
  `render_check` spend).
- `check(name)` returns a `{ok:false, error_type:"ToolBudgetExceeded"}`
  denial dict; `check_key(key)` raises `ToolBudgetExceeded`;
  `status()` is the readout.

**Enforcement — two surfaces:**

1. `ToolRunner._execute_gated` calls `tool_budget.check(name)` right after
   the storm breaker — an over-cap tool short-circuits before gates or the
   method run (covers flat-cost tools like `ada_render_video`).
2. `gemini_pool.generate` calls `tool_budget.check_key(tool)` before the
   first key attempt — over-cap raises before any paid call (fail-closed
   on caps, fail-open on gate bugs). Covers every token-metered aux call.

**Metering:** `gemini_pool.generate` now appends each successful call's
`usage_metadata` to the ledger as `source="gemini_aux"`, `tool=<label>` —
aux spend (web_search, cms_edit, doc_read, render_image/check, summaries,
rollups) is metered for the first time. `usage_tracker.record()` gained a
`tool=` param (defaults to source; all existing callers unchanged).

**Readout:** `ada_ops action='budget'` → `tool_budget.status()` —
`spend_by_tool` for today + `capped{cap,spent,remaining,keys}` + default
cap. Wired through the provider decl enum + description, DEFAULT_ADA_
INSTRUCTIONS hint, `tool_guide.yml`, the `ops_read` secondary-turn
carve-out (open read like `usage`), the `source` enum (`gemini_aux`
added), and `conversation_health().tool_budget`.

**Config:** `tools_loader.DynamicTool.daily_cap_usd` + manifest entries:
`ada_render_video: 5.0`, `ada_render_image: 2.0` (both env-overridable).

## Verification

- `tests/test_tool_budget.py` — **18 tests, all pass**: day scan
  (tool/source keys, alias aggregation, UTC boundary), cap resolution
  order, enforcement (check/check_key/runner-gate short-circuit +
  under-cap pass), readout (`ada_ops action='budget'` + status shape),
  metering (`_record_usage` row shape, `generate()` pre-flight raise).
- Related modules: `test_lane_budget test_gemini_pool test_tool_runner
  test_tools_loader test_usage_tracker_gate test_tool_usage_report` —
  **379 passed**.
- `python3 scripts/tool-lint.py` — clean (69 pre-existing warnings).
- Manual smoke: seeded a usage.jsonl (web_search $0.27, render_video
  $1.60 flat, live $20.40) → `status()`/`ada_ops budget` reported correct
  per-tool totals, alias aggregation, and `remaining_usd` (video 5.0−1.6
  = 3.4).

Note: test env here lacked pytest/deps — used a scratch venv at
`/tmp/ada-venv` (pytest, httpx, pyyaml, websockets, pillow, numpy).

## Behavior changes to be aware of

- `lane_budget.month_spend_usd` sums ALL ledger rows — `gemini_aux` rows
  now count toward the lane monthly ceiling (previously invisible aux
  spend). Correct accounting; a lane near `ADA_LANE_MONTHLY_BUDGET_USD`
  may trip sooner — intended conservative direction.
- Caps share the `ADA_USAGE_LOG` switch: ledger `off` ⇒ spend scans 0 ⇒
  caps never trip (metering and enforcement off together).
- Live voice and posture/clutter verifiers are not under per-tool caps
  (own call paths); Live keeps the lane monthly ceiling. They do appear
  in `spend_by_tool` and accept env caps for the readout.

SSOT trail: `docs/ssot/jobs/ada/2026-11-25-tool-daily-spend-caps.yml`.
Card pipeline (`pipeline: ci`) could not be checked — the card YAML is
not in this worktree; card-pipeline not run.

lessons:
- usage_tracker.entry_cost_usd is token-derived ONLY — flat-cost rows
  (Veo 'cost' field) must be added separately when summing spend.
- gemini_pool.generate already carries a tool= label on every aux call —
  the cheapest correct metering point is inside generate(), not per tool.
- This dispatch host has no project venv/pip; `python3 -m venv /tmp/x &&
  pip install pytest httpx pyyaml websockets pillow numpy` was enough to
  run the unit suite (dep_stubs covers google-genai/fastapi).
