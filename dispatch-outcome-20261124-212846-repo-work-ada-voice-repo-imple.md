# dispatch outcome — ada-capability-verb-wiring (ada-voice repo work)

## What changed

Consumer half of chaba `docs/kb/ada-tools-verb-contract.md` implemented:

- **`backend/tools.d/ada_capabilities.py`** (new) — tools.d drop-in,
  `policy=read`, `secondary_allowed=true`, `timeout_s=5`. Loads the
  generated catalog snapshot once per file mtime (cache keyed on
  path+mtime_ns), resolution order:
  1. `backend/data/ada-tools-snapshot.json` — deploy-refreshed runtime
     copy (gitignored via `data/`)
  2. `backend/generated/ada-tools-commands.json` — vendored, committed
  Env override `ADA_CAPABILITIES_SNAPSHOT` for tests.
  - No topic → deterministic grouped summary (~9 groups: memory & voice,
    calendar/tasks/plans, home/sensors/weather, cameras/screens/displays,
    kanban/jobs/hosts, docs/wiki/boards, news/reports/data,
    media/rendering, other) + "as of" provenance + offer line.
  - `topic=` → detail mode: exact tool, retired/absorbed name
    (merged_into honesty via `aliases` + `families[].absorbed`), family
    key, or keyword match over name/purpose/triggers.
  - Contract failure modes honored: missing/corrupt → `ok:false`
    "capability list not generated"; `schema` major ≠ 1 → refuse;
    `source.behind>0` or `generated_at` >30d → answer with staleness
    caveat + warning log.
  - **Producer quirk handled**: the catalog emits `tools[]` rows for
    every tools.d module DECLARATION including non-manifest
    implementation seats (wp_post/wp_chat_page/cpt_post/place_report
    behind manifest `wp`). Consumer filters alias keys out of the
    capability surface — absorbed names are never presented as
    capabilities, per contract.

- **`backend/generated/ada-tools-commands.json`** (new, committed) —
  vendored snapshot regenerated from THIS worktree's declarations via a
  patched copy of chaba's `scripts/ada/tools-commands-catalog.py`
  (REPO pinned to chaba for SSOT reads, OUT paths redirected; the
  canonical chaba outputs were not touched). 79 rows / 76 unique names,
  includes `ada_capabilities` itself and the post-consolidation `wp`
  surface.

- **`backend/tools.d/manifest.yml`** — `ada_capabilities` row added.

- **`backend/tool_guide.yml`** — error-path usage entry.

- **`backend/realtime_provider.py`** — one routing line in
  `DEFAULT_ADA_INSTRUCTIONS`: capability asks → `ada_capabilities`, never
  recite the tool list from memory.

- **`tests/test_ada_capabilities.py`** (new) — 8 tests, all pass
  (summary groups/counts, detail exact/retired/family/keyword/miss,
  schema-bump refusal, missing file).

- **`tests/scenarios-live/ada_capabilities.yaml`** (new) — smoke
  scenario ("what can you do" + "tell me about kanban") so the tool is
  scenario-covered; no `coverage_debt` entry needed. tool-lint clean
  (72 declared, cap 115).

- **`scripts/deploy-ada.sh`** — new gate 3c after tool-lint: runs
  `tools-commands-catalog.py --check` in the chaba checkout with
  `ADA_VOICE_REPO=$REPO` (hard `die` on STALE — a declaration edit that
  forgets the regen fails loudly), then copies the fresh JSON to
  `backend/data/ada-tools-snapshot.json`. Skips with a log line when no
  chaba checkout exists. `bash -n` clean.

- **`docs/ssot/jobs/ada/2026-11-24-capability-verb-wiring.yml`** — job
  record with findings/decisions/verify.

## Result / how to verify

- `python3 -m pytest tests/test_ada_capabilities.py -x -q` → 8 passed
  (run under a throwaway venv; this host has no project .venv).
- `python3 scripts/tool-lint.py` → clean.
- Manual smoke (no pytest needed): point
  `ADA_CAPABILITIES_SNAPSHOT` at the vendored JSON and `await run(None)`
  — verified: "I have 72 capabilities — 38 core + 34 add-on — as of
  2026-11-24" with grouped list; `topic=wp_post` answers "retired —
  merged into wp".
- Live done-condition: after merge+deploy, ask Ada "what can you do" —
  she calls `ada_capabilities` (scenario
  `tests/scenarios-live/ada_capabilities.yaml` proves routing).

## Follow-up required before next deploy (IMPORTANT)

The new `--check` gate is a HARD stop. Chaba's committed
`docs/kb/ada-tools-commands.{md,json}` are currently STALE vs this
tree (verified: `--check` exits 1). Before the next `deploy-ada.sh` run,
regenerate + commit them in the chaba checkout:

    cd ~/CascadeProjects/chaba && python3 scripts/ada/tools-commands-catalog.py

then commit `docs/kb/ada-tools-commands.*` through the normal chaba
flow. Until then every deploy aborts at gate 3c (by design — that is
the "fails loudly" the card asked for).

## lessons:

- The catalog producer counts every tools.d module DECLARATION as a
  tool row, including non-manifest implementation seats behind an
  alias canonical — consumers must filter `aliases` keys out of the
  capability surface or retired names get listed as live tools.
- This dispatch host has no project .venv/pytest; a throwaway
  `python3 -m venv /tmp/... && pip install pytest` runs the stdlib-only
  tool tests fine.
- Adding a hard deploy gate that reads another repo's committed
  generated files creates a cross-repo ordering dependency — flag the
  required producer-side regen prominently in card comms or the next
  deploy dead-stops.
