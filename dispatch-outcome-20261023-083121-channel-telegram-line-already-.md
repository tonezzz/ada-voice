# dispatch outcome — text-lane-provider-routing (2026-10-23)

## What the card asked

`channel=telegram|line` sessions on `/ws` ran the full Gemini Live audio
pipeline for text-only use. Route them to `OpenRouterTextProvider` (the
proven degraded path) so relay turn cost drops to per-call REST tokens;
voice/chatui-voice stays on Live. Caveat: confirm the text provider can
drive function calls (chat_send, cms_read, memory) or gate routing.

## Audit result (caveat resolved — no gating needed)

`OpenRouterTextProvider(tools=True)` already drives real function calls:
it harvests the identical lane-filtered `function_declarations` +
`system_instruction` from a throwaway
`GeminiLiveProvider._session_config()` probe, then runs a Gemini REST
tool loop via `gemini_pool.generate` (LiteLLM gateway "chat" class,
paid→free→OR→lite chain, per-lane spend tags) with the same
`tool_runner.execute` dispatch + pinned owner/session identity. Proven
in production by `channel=web` and covered by
`test_text_mode_tool_loop`. The OR free-model leg is the fallback when
Gemini REST fails — tool history flattens to text there (safe degrade).

Prompt caching needs no code: Gemini 2.5+/3.x implicit context caching
hits the ~19k-token static prefix (instructions + decls) automatically
at cache rates. Explicit OR-side `cache_control` was rejected — the OR
leg is already free models, and unsupported fields risk 400s on cheap
chain models.

## Changes (uncommitted, in this worktree)

- `pwa_server.py` — new `_wants_text_provider(mode, channel, caller)`
  helper + `_TEXT_ONLY_CHANNELS` env set (default `telegram,tg,line`).
  `/ws` now routes relay channels to the REST text provider by channel
  name — no opt-in param needed. `?mode=live` is an escape hatch
  (debug/rollback); `ADA_TEXT_ONLY_CALLERS` still wins unconditionally.
  Reconnect loop gains a hard `if _text_only: break` so a text lane can
  never upgrade into billed Live minutes mid-session.
- `backend/openrouter_provider.py` — added `upstream_dead()`,
  `upstream_close_code()`, `is_stalled()`, `resumption_handle`. The pwa
  `stall_watchdog` probed these on every session and died on
  `AttributeError` for every text session (silently — teardown
  suppresses task exceptions); it now survives and the speech-stall
  nudge can fire on text lanes.
- `backend/line_relay.py` — `&mode=text` now unconditional (was gated on
  `LINE_TEXT_MODE` env — the actual gap this card hit).
- `backend/tg_relay.py` — removed dead `TG_TEXT_MODE` block (`mode=text`
  was already hardcoded in the URL; the env only duplicated the param).
- `tests/test_text_lane_routing.py` — 10 tests: channel routing,
  mode= precedence, callers-list precedence, env override, watchdog
  probe surface.
- `.env.example`, `docs/ssot/ssot.channels.yml`,
  `docs/ssot/jobs/ada/2026-10-23-text-lane-provider-routing.yml` —
  docs/config/job record.

## Deliberately out of scope

`channel=chat` (PWA chat card) is the same Live-burn situation but not
in the card's scope — add `"chat"` to `ADA_TEXT_ONLY_CHANNELS` to extend
the routing after a soak.

## Verification

- `pytest tests/` — 1630 passed, 1 skipped (CI deselects applied).
- `scripts/tool-lint.py` — clean (61 pre-existing warnings).
- `scripts/scenario-schema-check.py` — 0 violations.
- Post-deploy: a LINE/TG turn should log
  `text-mode provider (channel=line|telegram caller=...)` and emit
  `text_mode_reply` ops events; usage ledger should show no Live minutes
  on relay sessions. Rollback: `ADA_TEXT_ONLY_CHANNELS=""` or per-client
  `?mode=live`.

## Notes for the operator

- Nothing committed/pushed/deployed — worktree only.
- A throwaway `.venv/` (gitignored, CI-light deps, no torch) was created
  in the worktree to run the suite; safe to delete.
- Board comment posted to `text-lane-provider-routing` with the summary.
