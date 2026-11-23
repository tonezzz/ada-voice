# Dispatch outcome — ada-prompt-slim

**Card:** ada-prompt-slim (ADA-1282) · **Runner:** tony-dell ·
**Branch:** `dispatch/20261123-105302-ada-voice-backend-realtime-pro` (not pushed)

## What changed

- `backend/realtime_provider.py` — `DEFAULT_ADA_INSTRUCTIONS` compacted
  **14016 → 6999 chars (50.06% shorter), 78 → 46 lines**. Every paragraph
  is now a one-line rule; the duplicated `DONE MEANS DONE` paragraph
  (the block appeared twice with near-identical text) merged into one.
  All duties renamed to their canonical chaba AGENTS.md section names and
  tagged `(AGENTS.md)` with a header pointer: "(AGENTS.md) tags mirror
  chaba AGENTS.md duties — keep both in sync (the parity rule)."
  - `Correction duty` → `Operator-correction & education duty`
  - `Request capture` → `Open-ask capture`
  - `Idea momentum` + `Idea intake routing` → `Idea-pipeline` (merged)
  - `Field-ownership` — NEW line; the KNB-0453 rule (humans own workflow
    fields, producers own content fields) existed in chaba AGENTS.md but
    was missing from the prompt — added since the card names it a
    must-keep duty.
  - `Report integration` → `Report-integration`, `Spec-gathering`,
    `Procedure-lookup`, `Kanban-first teamwork`, `Project steward duty`
    (ssot.project-lifecycle) — all still named.
  - Non-duty rules compressed but kept: LANGUAGE FIDELITY, POLITENESS
    PARTICLES, NO ROUTINE CLOSER, SPEECH IS PLAIN TEXT, BE BRIEF, ERROR
    TRUTHFULNESS, BAD CALLS, CONNECTION RESET, TITLE/PLAYING-STATE,
    NEWS vs MEDIA, DATE ECHO, POST-MIDNIGHT AMBIGUITY, word coaching,
    live-status line (frozen verbatim for test_card_ops_instructions).
- `AGENTS.md` — new "Ada prompt parity" section: pointer to chaba's
  AGENTS.md as canonical long-form + the do-not-re-expand rule. Pointer
  only — no duty text copy (the prior Jules attempt was flagged for
  duplicating the duties section 3x here).
- `docs/ssot/jobs/ada/2026-11-23-ada-prompt-slim.yml` — job trail.

## Parity

Ada's duty names now match the canonical chaba AGENTS.md sections
verbatim; semantics unchanged → no chaba-side edit needed (and chaba is
outside this worktree's write scope anyway). Both sides now point at
chaba AGENTS.md as the single long-form source.

## Result

- `python3 scripts/tool-lint.py` — clean (60 pre-existing warnings)
- `pytest tests/ -x -q` — **1854 passed, 7 skipped** (baseline before
  edit: identical)
- `pytest tests/test_card_ops_instructions.py` — pass (frozen
  live-status line kept verbatim)
- DONE-CONDITION: ~50%+ shorter ✓ (50.06%), all duties named ✓,
  parity maintained ✓, unit suite green ✓

## How to verify

- `grep -n 'DEFAULT_ADA_INSTRUCTIONS' backend/realtime_provider.py` then
  read the block (now lines 3043–3088).
- Live `education_correction` scenario is a scenarios-live probe needing
  a running ada-dev service — deferred to post-deploy smoke cadence
  (deploy only via `deploy-ada.sh` on idc03). The offline scenario tier
  (tests/test_scenarios.py) passes.

## lessons:
- board /comment POSTs can take >10s through the flock/Caddy path — use --max-time ≥90; a timed-out POST may still have landed (dedup catches the retry).
- Prior ada-prompt-slim runs died from provider rate-limits mid-flight, not code — the work itself was never wrong; Jules' only real defect was duplicating the duties section 3x in AGENTS.md (use a pointer, not a copy).
- tests/test_card_ops_instructions.py freezes three exact substrings in the Live-status line — keep it verbatim when slimming.
- Worktree has no .venv; system python3 + pytest works via tests/dep_stubs (full suite ~4min, 1854 tests).
- "field-ownership" in the card maps to the KNB-0453 rule in chaba's AGENTS.md — fetch it via the github MCP (tonezzz/chaba AGENTS.md) since the local checkout is outside the worktree.
