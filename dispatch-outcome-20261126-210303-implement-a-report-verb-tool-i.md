# Dispatch outcome — ada-voice-fleet-jobs-verb

Card: `ada-voice-fleet-jobs-verb` (chaba kanban, ref ADA-0041).
Contract: chaba `docs/kb/ada-report-verb-contract.md`.

## What changed

Registered tool: **`ada_fleet_jobs`** (backend/tools.d drop-in,
manifest `policy: read`, `secondary_allowed: true`, `timeout_s: 10`).

- `backend/tools.d/ada_fleet_jobs.py` (new) — the report-verb read
  path. Exactly one MDDB read per call:
  `get_document("ada-cms-pages", "fleet-jobs-report", "en")` via the
  `runner.context.mddb` facade (`runner.mddb` fallback for bare test
  fakes). Freshness gate on `meta.updated` vs `meta.fresh_for`
  (4h default, parsed with `backend/report_meta.fresh_for_seconds`):
  - fresh → 2–4 sentence digest: `meta.summary` leads, then a parsed
    health sentence (failing/late timer counts + names across
    `## Host:` sections, stale runner heartbeats) and LiteLLM 24h
    spend when rows exist.
  - stale → same body prefixed `As of <updated UTC>` + "the
    fleet-jobs-report producer may be down".
  - missing page / unreachable store / raised read → `ok:true,
    generated:false`, "the fleet jobs report hasn't been generated
    yet" + offer to card it. Unparseable `meta.updated` → answer +
    "treat this as unverified". No mddb (guest mode) → `ok:false`.
  - optional `focus=` (spend|timers|lanes|queue|hosts, generous alias
    matching, unknown → overview) for the contract's section intents.
  - Parser tolerates both page generations — the current
    (`## Runner Fleet`, `### Timer States` + bad-timer table, lane /
    LiteLLM tables) and the 2026-11-20 layout (`## Live Queue` Pos/Card
    table, emoji 🟢🟡🔴⬛ counts, `Attention timers`) — verified
    live-fire against the real fetched page.
  - Never runs the aggregator, never touches board-api /cards|/queue,
    psql, ssh, systemctl, or a sub-agent — contract enforced by
    construction (the module's only I/O is the single get_document).
- `backend/tools.d/manifest.yml` — manifest row for `ada_fleet_jobs`.
- `backend/tool_guide.yml` — error-path usage entry (quote `answer`,
  relay the stale caveat, never recompute).
- `backend/tools.d/ada_capabilities.py` — `_GROUPS` gains
  `ada_fleet_jobs` under "kanban, jobs & ops" (would otherwise land in
  misc).
- `tests/test_ada_fleet_jobs.py` (new, 14 tests) — no-network adapter
  tests: fresh / stale / missing / unreachable / empty / unparseable
  updated, exact contract read target asserted
  (`ada-cms-pages/fleet-jobs-report/en`), all six focus modes, legacy
  page layout.
- `tests/scenarios-live/fleet_jobs.yaml` (new) — smoke-tier live
  scenario (`needs_tools: [ada_fleet_jobs]`), keeps the tool out of
  coverage_debt.
- `docs/ssot/jobs/ada/2026-11-26-fleet-jobs-verb.yml` — job trail.

## Verification

- `python3 scripts/tool-lint.py` — clean (73 declared: 38 builtin +
  35 tools.d, 67 scenario-covered, cap 115).
- `python3 scripts/scenario-schema-check.py` — 213 files, 0 violations.
- `~/CascadeProjects/ada-voice/.venv/bin/python -m pytest tests/ -x -q`
  — **2042 passed, 8 skipped** (5m36s).
- Live-fire: ran `run()` against the real
  `ada-cms-pages/fleet-jobs-report` doc fetched from MDDB — stale gate
  fired correctly (page updated 2026-11-20 > 4h → "As of 2026-11-20
  13:10 UTC … producer may be down"); all focus modes produced
  speakable digests.

## Notes / how to verify post-deploy

- Deploy is operator-side (`deploy-ada.sh` on idc03); nothing was
  pushed or deployed from this worktree.
- Post-deploy check: ask Ada "how are jobs" on ada-ha-tony — answer
  should cite the page content; if the producer is >4h stale the
  answer carries the "as of" prefix + may-be-down flag; if the page is
  absent the answer is the contract's "hasn't been generated yet" +
  card offer.
- The card has `action: manual` and no `pipeline: ci` — card-pipeline
  not run.

lessons:
- tools.d modules load by file path but `from backend import
  report_meta` works (kanban.py precedent) — reuse report_meta's
  first()/fresh_for_seconds() instead of re-parsing meta lists.
- mddb get_document swallows transport errors AND 404s into None —
  the contract's "missing / unreachable" single answer path is free.
- The fleet-jobs-report page has TWO live layouts (current
  Runner-Fleet/Timer-States vs the 2026-11-20 Live-Queue/emoji-count
  generation); section-tolerant regex parsing beats format pinning.
- _sentence() capitalizes the first char — host/timer names are
  case-significant, so clause builders that lead with a hostname need
  the period-only _term() variant (and _freshness must not re-capitalize
  the composed body).
