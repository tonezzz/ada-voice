# dispatch-outcome — ada-expert-consult-tool (attempt 1, idc02)

## What I found

The card's code work was **already merged** before this dispatch ran.
Card comms (Tony triage 2026-11-24 14:59) recorded both PRs merged:
ada-voice#12 squash `e6c83ebe93ad` ("Ada expert-consultation tool") and
chaba#180 (`8efa20072747`, consult gateway aliases). This worktree's
`origin/main` contains the merge, so there was nothing to re-implement.

Prior failure was `FAILED_PRECONDITION` — the Jules lane EOL
(2026-11-12); the card has since been rerouted to devin runners.

## What I did

1. **Verified merged implementation against the card spec** —
   `backend/tools.d/ada_consult.py` covers every decision: dedicated
   `LITELLM_KEY_CONSULT` (no shared-key fallback), allowlisted
   `consult-flash|consult-pro|consult-claude` aliases, text-lanes-only
   (`current_channel` unset → refuse, so voice/PWA-voice can't hold a
   consult), double ada-dev gate (manifest `instances: [ada-dev]` +
   `ADA_INSTANCE_ID=ada-dev`), `ADA_CONSULT_MAX_{USD,TOKENS,WALL_S,
   QUESTION_CHARS,CONTEXT_CHARS}` caps with max_tokens floor 2048
   (ADA-1088 reasoning-truncation gotcha), fail-closed cost gate
   requiring per-model USD/MTok rate env vars, `context_class=curated`
   + `memory_write_guard.scan` + `is_transcript_shaped` refusals,
   ops events `consult_requested/completed/failed` with question as
   sha256 only, no memory write-back.
2. **Re-ran the gates in this worktree** (fresh `.venv`, gitignored):
   - `python3 scripts/tool-lint.py` → clean (69 warnings, none new)
   - `.venv/bin/python -m pytest tests/ -x -q` → **1971 passed,
     4 skipped, 31 subtests** (~3m20s)
3. **Closed the one gap**: `.env.example` had no consult section —
   added the full env contract (key, model alias, per-call caps, the
   `ADA_CONSULT_{FLASH,PRO,CLAUDE}_{INPUT,OUTPUT}_USD_PER_MILLION`
   rate card, guard summary) so the ada-dev env file can be configured
   without reading source.
4. **Wrote the trail**:
   `docs/ssot/jobs/ada/2026-11-24-ada-expert-consult-verify.yml` —
   verification record + activation checklist.

## Result / how to verify

- Diff in this worktree: `.env.example` (+26 comment lines) and the new
  jobs yml. No code changes needed — merged code is spec-complete.
- Verify: `git diff .env.example`, read the jobs yml; rerun
  `scripts/tool-lint.py` and `pytest tests/test_ada_consult.py`.

## Still blocked (non-code, already flagged on the card)

- `LITELLM_KEY_CONSULT` must be minted at the provider — Tony action;
  card requests already answered: $5/30d key cap, $0.10/call ceiling
  (both match the code defaults).
- ada-dev deploy/restart on idc03 with `LITELLM_KEY_CONSULT` +
  `ADA_CONSULT_*` env (deploy-ada.sh gates), then the 3-call live
  smoke (easy/ambiguous/hard) + micro-bench on the messy/ambiguous
  fixtures. Until then every consult call refuses cleanly by design.
- Gateway probe from this host: `http://100.102.134.91:4000/health/
  liveliness` → 200; `/v1/models` → 401 without a key (expected).

lessons:
- Cards rerouted after provider EOL can arrive with the implementation already merged — check `git log origin/main` and card comms before writing code.
- Dispatch worktrees have no `.venv`; `python3 -m venv .venv && pip install pytest httpx pyyaml websockets fastapi 'uvicorn[standard]' pillow qrcode numpy faster-whisper soundfile` is enough for the full suite (dep_stubs covers google-genai; heavy deps like torch aren't needed by tests).
- Repo `.env.example` is the canonical env contract — new tools' env vars must be documented there or the deploy step has no checklist.
- jobs/*.yml files are loose YAML — quote list items containing `: ` or the file won't parse.
