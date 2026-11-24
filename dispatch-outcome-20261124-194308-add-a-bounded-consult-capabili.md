# dispatch-outcome — ada-expert-consult-tool (attempt 2, tony-dell)

## What I found

Same state as attempt 1 (idc02, merged at 19:26): the implementation is
already on `origin/main` — `backend/tools.d/ada_consult.py` + manifest
entry + `tests/test_ada_consult.py` from ada-voice#12 (squash
`e6c83ebe93ad`), consult gateway aliases from chaba#180 (`8efa20072747`).
This worktree's HEAD == origin/main at dispatch start; the card had been
auto-closed by merge-sweep at 19:38 then re-queued at 19:41.

I re-audited the merged code against the spec rather than trusting the
prior verify:

- **Channel gate is correct end-to-end**: `execute(channel=...)` sets
  `_CALLER_CHANNEL` only for relay/text sessions; voice PWA connects
  without `?channel=` so `current_channel` is `None` and consult refuses
  — TEXT LANES ONLY holds (webchat passes `channel=web`, tg/line pass
  theirs).
- **Cost-rate lookup order is safe**: the pre-call bound runs on the
  alias name before `gemini_pool._gateway_model()` remaps, and
  `_RAW_TO_CLASS` contains no `consult-*` keys so aliases pass through
  to LiteLLM unchanged — post-call `_estimate_cost` still resolves the
  per-model rate env vars.
- Manifest `instances: [ada-dev]` + `ADA_INSTANCE_ID` double-gate,
  dedicated `LITELLM_KEY_CONSULT` with no shared-key fallback,
  allowlisted aliases only, fail-closed rate card, secret/transcript
  guards, ops events with sha256-only question, no memory write-back —
  all present.

## What I changed (this run's diff, commit `aa429be` on the dispatch branch)

1. **`backend/tool_guide.yml`** — added the missing `ada_consult` entry.
   Until `LITELLM_KEY_CONSULT` is minted every consult refuses; without a
   guide entry the model got a bare error with no narration guidance.
   `_usage_hint` now attaches scope rules (text lanes, ada-dev soak,
   curated context, allowlisted aliases) plus explicit "expert line not
   live — do not retry or improvise" guidance on every refusal.
2. **`tests/scenarios-live/ada_consult_smoke.yaml`** — `tier: manual`
   scenario encoding the card's live smoke: 3 turns (easy / ambiguous /
   hard) each asserting `calls_any: [ada_consult]` + an `"ok": true` /
   `"answer"` tool result. `needs_tools: [ada_consult]` skips lanes where
   the tool isn't declared; manual tier keeps it out of smoke/full
   sweeps. Header documents the run command and the manual post-checks
   (ops events + LiteLLM spend ledger).
3. **`docs/ssot/ssot.tool-surface.yml`** — removed `ada_consult` from
   `coverage_debt`; the scenario is now its coverage reference
   (tool-lint warns on covered-but-listed-debt).
4. **`docs/ssot/jobs/ada/2026-11-24-ada-expert-consult-hardening.yml`** —
   trail: audit notes, changes, gates, remaining blockers.

## Gates (all green in this worktree)

- `python3 scripts/tool-lint.py` → clean (69 warnings, none new;
  coverage now 65 scenario-covered / 29 debt)
- `python3 scripts/scenario-schema-check.py` → 211 files, 0 violations
- `.venv/bin/python -m pytest tests/ -x -q` → **1971 passed, 4 skipped,
  31 subtests** (~3m06s). Note: this worktree had no `.venv` and the
  fresh `pip install` stalled on the loaded host; I ran the suite with
  the main checkout's existing venv
  (`~/CascadeProjects/ada-voice/.venv/bin/python`) from this worktree —
  deps identical, results valid. Removed the half-built `.venv` after.

## Still blocked (non-code, already flagged on the card)

- `LITELLM_KEY_CONSULT` minting — Tony credential action ($5/30d cap,
  $0.10/call ceiling already agreed on-card).
- ada-dev deploy/restart on idc03 with `LITELLM_KEY_CONSULT` +
  `ADA_CONSULT_*` env via `deploy-ada.sh` gates — dispatch mode forbids
  deploy and the checkout is outside this worktree.
- Live smoke + micro-bench — the smoke is now a one-command scenario
  (see jobs yml); the bench fixtures are chaba-side (ADA-1088) and need
  the live gateway anyway.

## How to verify

- `git show aa429be` — the 4-file diff.
- `python3 scripts/tool-lint.py` and
  `python3 scripts/scenario-schema-check.py` — both clean.
- Post-deploy on ada-dev:
  `python3 scripts/scenario-live.py tests/scenarios-live/ada_consult_smoke.yaml --url ws://127.0.0.1:8003/ws --api-key "$ADA_API_KEY" -v`
  then check ops events (`consult_completed` x3) and the LiteLLM spend
  ledger for `LITELLM_KEY_CONSULT`.

lessons:
- Dispatch worktrees can borrow the main checkout's `.venv` (`~/CascadeProjects/ada-voice/.venv/bin/python -m pytest` from the worktree cwd) when a fresh `pip install` stalls — deps are identical, no need to rebuild.
- A tools.d tool without a `tool_guide.yml` entry still lints clean but gives the model zero narration guidance on refusal — check guide coverage, not just lint.
- `tier: manual` + `needs_tools:` is the right shape for live-only smoke scenarios: skips undeclared lanes, stays out of automated sweeps.
- scenario coverage counts `needs_tools`/`needs_any` references — adding one makes the tool "covered", so the matching `coverage_debt` SSOT entry must be removed same-commit (lint warns otherwise).
