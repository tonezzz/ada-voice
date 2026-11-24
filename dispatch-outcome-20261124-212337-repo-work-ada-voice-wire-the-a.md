# Dispatch outcome — ada-capabilities-verb-wiring

Card: `ada-capabilities-verb-wiring` (chaba board). Repo: ada-voice.
Contract: chaba `docs/kb/ada-tools-verb-contract.md` (schema 1).

## What changed (worktree, uncommitted — dispatcher checkpoints)

- **`backend/tools.d/ada_capabilities.py`** (new) — the "what can you do"
  read path. `DECLARATION` blurb 229 chars / 4 lines (within the
  `desc_max_*` contract); optional `topic=` param for detail mode.
  - mtime-keyed snapshot loader (`ADA_TOOLS_SNAPSHOT` env override;
    default `backend/data/ada-tools-snapshot.json`) — same cached-read
    pattern as `_tool_guide()`, keyed on `(path, mtime_ns)` so a
    deploy-time file swap is picked up without restart.
  - Overview answer = contract template: `I have N capabilities — M
    core + K add-on tools — as of <head_date>.` + 10 deterministic
    groups (family/name-prefix rules, first-match, `misc` fallback) +
    "Ask me about a specific tool or area for details." Core =
    builtin-layer names, add-on = dropin-only; multi-layer rows dedupe
    (builtin > dropin > guest).
  - Canonical names only: any `tools[]` name that is an `aliases` key
    is filtered — matters because the producer emits rows for absorbed
    tools.d implementation seats (wp_post/cpt_post/place_report/
    wp_chat_page post-merge).
  - Detail mode `topic=` resolves in order: exact tool → absorbed-alias
    redirect ("'cctv_snapshot' is an old name — it routes to
    ada_camera_snapshot") → area/group label → merge-family key →
    unique name substring → honest `ok:false` with hint.
  - Failure modes per contract: missing/corrupt → `ok:false`
    "capability list not generated — run the catalog producer";
    `source.behind > 0` or `generated_at` > 30 d → answer continues
    with a stale caveat echoing snapshot fields only (byte-deterministic
    per snapshot) + `logger.warning`; `schema != 1` → refuse with the
    version note.
- **`backend/tools.d/manifest.yml`** — `ada_capabilities` row:
  `policy: read`, `secondary_allowed: true`, `timeout_s: 5`.
- **`backend/tool_guide.yml`** — `ada_capabilities:` error-path entry
  (quote `answer`, relay the as-of/stale caveat, never fabricate on
  ok:false, absorbed names resolve but aren't surface).
- **`scripts/deploy-ada.sh`** — new step 2b after the ff-merge: copies
  `$CHABA_REPO/docs/kb/ada-tools-commands.json` (default
  `~/CascadeProjects/chaba`, `CHABA_REPO` override — same convention as
  trip_calendar) to `backend/data/ada-tools-snapshot.json` via
  `mkstemp`+`os.replace`, validates `schema == 1` first. Missing or
  invalid source → WARN, never blocks deploy (tool degrades to
  ok:false by design). `backend/data/` is already gitignored (`data/`),
  so the snapshot stays a runtime artifact — single transport, one
  direction.
- **`tests/test_ada_capabilities.py`** (new) — 14 tests on a fixture
  snapshot: grouped overview + counts + canonical-only filtering +
  determinism, detail by tool/family/area/alias-redirect, unknown
  topic, missing/corrupt/schema-bump failures, stale caveat, empty
  surface, and a skip-guarded test against the real shipped snapshot.
- **`tests/scenarios-live/capabilities.yaml`** (new) — `tier: smoke`,
  `transport: text`, `needs_tools: [ada_capabilities]`; turn 1 "Ada,
  what can you do?" expects the call + `"ok": true`; turn 2 checks
  `topic=` routing ("from your own capability list, what does the
  kanban tool do?"). Leaves `coverage_debt` at creation.
- **`docs/kb/ada-tools-snapshot.md`** (new) — transport runbook:
  producer → deploy copy → consumer, ops notes, refresh-without-deploy.

## Gates (all green in this worktree)

- `python3 scripts/tool-lint.py` → clean; surface 72 declared
  (38 builtin + 34 tools.d), cap 115, `ada_capabilities` scenario-covered
  (was 65 covered → 66).
- `python3 scripts/scenario-schema-check.py` → 212 files, 0 violations.
- `.venv`-borrowed pytest (`ada-voice-wt-wiki-confirm-flag/.venv`,
  python 3.14, pytest 9.2.0 — this host has no pip/venv of its own):
  `pytest tests/ -x -q` → **1989 passed, 5 skipped, 31 subtests**
  (163s). Includes the 13 new unit tests.
- Live-fire check against the real chaba snapshot
  (`ADA_TOOLS_SNAPSHOT=~/CascadeProjects/chaba/docs/kb/ada-tools-commands.json`):
  overview answered in **3.5 ms** — "I have 68 capabilities — 37 core +
  31 add-on tools — as of 2026-11-19." + stale caveat (snapshot is 7
  commits behind, generated pre-wp-merge). Detail modes verified for
  tool/family/group/alias/unknown.
- Deploy sync block exercised standalone against the real chaba JSON →
  valid schema-1 copy written atomically.

## Notes / follow-ups

- The chaba snapshot currently committed is stale (generated 2026-11-23,
  `behind: 7`, pre-`wp`-merge) — the producer reruns on decl change per
  its own triggers; once regenerated, absorbed names (wp_post et al.)
  drop out of `tools[]` surface via the aliases filter automatically.
- First deploy after merge ships the snapshot; until then the tool
  answers `ok:false` honestly (contract failure mode) and the smoke
  scenario would flag it — expected ordering.
- `coverage_debt` unchanged — the new tool never entered it.

## How to verify

- `git diff` / untracked files listed above.
- `python3 scripts/tool-lint.py && python3 scripts/scenario-schema-check.py`
- `pytest tests/test_ada_capabilities.py -x -q`
- Post-deploy on any lane:
  `ADA_TOOLS_SNAPSHOT=backend/data/ada-tools-snapshot.json python3 -c "import asyncio,importlib.util; ..."`
  or ask Ada "what can you do?" / run
  `scripts/scenario-live.py tests/scenarios-live/capabilities.yaml`.

lessons:
- tools.d producer scans EVERY module DECLARATION (incl. non-manifest absorbed seats), so the consumer MUST filter tools[] names that are aliases keys — not just rely on the producer emitting surface-only.
- `data/` in .gitignore matches `backend/data/` too — deploy-shipped artifacts under backend/data need no extra ignore rule.
- This dispatch host (tony-macbook) has no pip/venv; borrow a sibling worktree's `.venv/bin/python -m pytest` from the worktree cwd — deps identical, suite ran green (1989).
- tony-macbook has no `~/CascadeProjects/chaba` producer checkout guarantee — deploy-ada.sh warns-not-blocks on a missing snapshot source by design; verify `$CHABA_REPO` on each deploy host.
