# Dispatch outcome — live-voice-burn-context-replay (attempt, idc02)

Card: `live-voice-burn-context-replay` (ada-voice, ADA-0631). Done-condition:
documented analysis or fix for the ada-dev whale + t-ctx test pollution,
verified against the recorded token metrics.

## Headline findings

- **"Implement prompt-caching" is not implementable on the Live API.**
  `LiveConnectConfig` has no `cached_content` (upstream issue
  googleapis/python-genai#2102); Google staff confirm neither explicit
  nor implicit caching for gemini-3.x-live — system_instruction + tool
  declarations bill at full text rate EVERY turn; `cached` rows are
  zero across 5 weeks of usage.jsonl. REST text lane already gets free
  implicit caching; explicit cachedContents 404s on the LiteLLM gateway
  (chaba-side). Full verdict in the job file.
- **Static prefix measured: ~19.3K tokens** (system_instruction 36,001
  chars ≈ 8,060 tok + 72 lazy tool decls ≈ 11,271 tok, cl100k proxy).
  Live API `prompt_token_count` is the whole session context per turn —
  the prefix re-bills every turn, which reconciles the dig's numbers:
  median 89K in / ~4-5 turns; ada-dev 28.7M/1381 turns = 20.8K in/turn.
- **A parallel attempt (tony-omen, merged to origin/main mid-session)
  already landed the stronger half**: usage_tracker lane-identity gate
  kills the t-ctx pollution hole for ALL non-pytest runners + a
  re-measure showing the ada-dev whale collapsed to 0-5.5M/day residual
  automated-session bursts. My branch fast-forwarded onto it; work is
  complementary, no file overlap.

## What changed (commit f15491f on dispatch/20261124-080026-...)

- `backend/realtime_provider.py` — `send_context_prime`: ABC default
  delegates to `send_text_turn`; `GeminiLiveProvider` overrides to
  append via `_send_context_note` (`turn_complete=False`) — the session
  prime lands as context with NO billed response turn.
- `pwa_server.py` — `_prime_session` honors `ADA_PRIME_AS_CONTEXT=1`
  (default OFF): prime as silent context instead of a billed turn.
  Saves ~19K in + greeting out per fresh connect (~13% of dig-day fleet
  in at 310 sessions/24h). Trade-off: no proactive greeting — the
  prime's greet directive is honored on the user's first turn. Text
  lanes unaffected (OR provider's `_is_context_turn` already treats
  "(system)" primes as free context).
- `tests/__init__.py` — pins conftest's env for `python -m unittest
  tests.test_x` runs (determinism parity; lane gate is primary fix).
- `tests/test_provider_events.py` — turn_complete=False + not-connected
  tests for the new method.
- `.env.example` — documents ADA_PRIME_AS_CONTEXT.
- `docs/ssot/jobs/ada/2026-11-24-live-voice-token-burn.yml` — full
  analysis: metrics reconciliation, prefix anatomy, caching verdict,
  ranked levers (prime-as-context > prefix diet > scenario
  transport:text flips > bench tagging).

## Verification

- pytest focused (provider_events incl. 2 new, context_budget,
  lane_budget, usage_tracker_gate, scenarios): 133 passed.
- Full suite: 1920 passed; only failures were relay-endpoint tests
  missing real starlette in the ad-hoc venv — all 42 pass with it
  installed (pre-existing env gap, unrelated).
- Pollution repro clean on all three runner styles: pytest (conftest),
  `python -m unittest tests.test_context_budget` (11 OK, no
  usage.jsonl), direct `python tests/test_context_budget.py` (11 OK,
  no usage.jsonl) — lane gate + pins double-cover.

## How to verify / next steps

- Post-deploy on ada-dev: set `ADA_PRIME_AS_CONTEXT=1`, open a /ws
  session, confirm the prime lands in context and the first user turn
  greets with identity; usage.jsonl should show one fewer `in` row per
  session and lower median session in-tokens. Then fleet-roll the flag.
- Sibling job's verify steps still apply for the pollution gate
  (zero non-hex session_ids / t-ctx rows in idc03 usage.jsonl).

lessons:
- Live API has NO prompt/context caching on any transport — don't burn
  dispatches "implementing" it; the levers are prefix size, billed turns
  per session, and session count.
- A parallel dispatch on the same card merged mid-session — always
  `git fetch` + check `git log --all` for same-card branches before
  finalizing; fast-forward and reconcile rather than duplicate.
- Live API prompt_token_count = whole session context per turn — the
  system_instruction + tool decls (~19K tok here) re-bill every turn;
  turn_complete=False context sends are the cheap injection path.
- t-ctx pollution had THREE runner styles (pytest/unittest/direct) —
  fixing only conftest left the hole open; gate at the ledger.
- Ad-hoc venv test runs fail on missing starlette/uvicorn — install
  real deps before judging relay-endpoint failures as regressions.
