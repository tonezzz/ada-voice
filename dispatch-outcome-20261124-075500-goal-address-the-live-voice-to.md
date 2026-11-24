# Dispatch outcome — live-voice-burn-context-replay (attempt 3)

Card: `live-voice-burn-context-replay` (ada-voice, ADA-0631). Prior
attempts died on provider rate limits, not code — this run completed the
analysis + fix the card's done-condition asks for.

## What I found (re-measured on idc03 usage.jsonl, 78.5k rows thru 11-23)

- **ada-dev whale is already collapsed.** Dig day 10-14: 41.6M in.
  Residual lane=dev: 0–5.5M/day in bursts (11-07..11-22), driven by ~42
  short (5–9 turn) Live sessions/day — automated scenario/probe cadence.
  Smoke tier is 44/47 `transport:text`; 3 smoke scenarios legitimately
  stay live; 153 full-tier scenarios still `transport:live` on weekly
  timers/benchmarks.
- **TEST-POLLUTION recurred.** The 10-14 conftest fix covers pytest
  only. A non-pytest runner wrote `t-ctx` rows daily 10-17→11-09
  (~4.36M fake in-tokens; exactly test_context_budget.py's five
  `_record_usage` calls at ~08:20 UTC, lane=""). It stopped 11-09 but
  the hole stayed open for any direct/`unittest` execution.
- **Prompt-caching is not implementable on either transport today.**
  Live API: `LiveConnectConfig` has no `cached_content` (google-genai
  2.23.0; upstream issue #2102); zero `cached` tokens in 5 weeks of
  ledger. REST lane: implicit caching is already free/automatic; explicit
  `cachedContents` 404s on the LiteLLM gateway (probed live — needs
  chaba-side gateway work).

## What changed

- `backend/usage_tracker.py` — implicit default `data/usage.jsonl` now
  persists only when `ADA_INSTANCE_ID` resolves a lane; unconfigured
  processes (tests outside conftest, ad-hoc shells) get in-memory-only
  ledger. Explicit `ADA_USAGE_LOG=`/`configure()` unchanged.
- `tests/test_context_budget.py` — `ADA_USAGE_LOG=off` pinned at module
  top (covers direct/unittest execution of the known polluter).
- `tests/test_usage_tracker_gate.py` — new gate coverage.
- `docs/ssot/jobs/ada/2026-11-24-live-voice-burn-recheck.yml` — full
  analysis + verdict + residual levers.

## Verification

- `pytest tests/ -x -q`: **1927 passed, 7 skipped** (full gate suite).
- Pollution repro: `env -u ADA_USAGE_LOG -u ADA_INSTANCE_ID
  PYTHONPATH=. .venv/bin/python tests/test_context_budget.py` in empty
  cwd → 11 tests OK, **zero files created** (was 5 ledger rows/run).
- Gateway probe: `POST /v1beta/cachedContents` → HTTP 404.

## How to verify post-deploy

Aggregate `data/usage.jsonl` on idc03 by day/session_id — expect zero
non-10-hex session ids and zero `t-ctx` rows going forward; lane=dev
Live-session count should keep falling as text transport expands.

lessons:
- pytest's conftest env pins do NOT cover `python tests/test_x.py` or
  `python -m unittest` — ledger/env guards belong in the module or the
  library, not only conftest.
- Gemini Live API has no prompt caching (no cached_content on
  LiveConnectConfig); check `cached` in usage.jsonl before assuming
  implicit caching hits — it reported zero for 5 weeks.
- LiteLLM gateway on idc03 lacks /v1beta/cachedContents (404) — explicit
  Gemini context caching needs a chaba-side gateway route first.
- Scheduled-looking daily usage.jsonl rows (exactly 5, ~08:20 UTC) were
  the tell for a non-pytest test runner; match row counts to the exact
  `_record_usage` call sites in the test file.
