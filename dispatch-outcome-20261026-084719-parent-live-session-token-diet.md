# Dispatch outcome — expression-call-batching (OPS-0509 token diet)

## What changed

Card option **(b) throttle**, extended with an instruction diet — the
instructed "call set_facial_expression once per reply" was the real cost
driver (17k calls/7d, ~28k billed context resend each; session
f245275fde ran 234).

`backend/realtime_provider.py`:
- New budgets, read per connect next to `ADA_TOOL_CALL_BUDGET`:
  - `ADA_EXPRESSION_TURN_BUDGET` (default 2) — max applied expression
    calls per turn (once-per-reply + one material tone update).
  - `ADA_EXPRESSION_MINUTE_BUDGET` (default 4) — rolling 60s window of
    *applied* calls, kept on the provider instance so it survives Live
    reconnects. `0` on either disables the tool outright.
- Over-cap calls get a soft ok:true result ("Expression unchanged — … do
  not call … unless the tone clearly demands it") and emit NO
  `ProviderEvent("expression")` — the face doesn't change, so the cap has
  teeth. Deliberately not ok:false: a failed result would count in
  `failed_results_this_turn` and taint the phantom-claim gate for a
  cosmetic refusal; a hard error would invite a billed retry. The first
  refusal per session posts ops event `expression_throttled`.
- Instruction paragraph rewritten: "call once per reply" → "every call is
  a full extra model round — call only on a material tone shift; most
  replies need no call at all".
- `tests/test_provider_events.py`: `ExpressionStormSession` + a test that
  the 3rd same-turn call is throttled (2 expression events, 3 responses).

Not done — and why:
- Option (a) (queue expressions in reply text) is infeasible on voice:
  native-audio sessions have no non-spoken output channel — reply text is
  only the audio transcription, so a marker would be spoken aloud.
- Option (c) (server-side classify / cheap side channel) would take
  expression choice away from the model — deferred; reconsider if the
  caps don't move the metric.

## Result / verify

- Focused tests: 109 passed (provider_events incl. new storm test,
  text_channel, dead_turn, transcript_leak, tool_audit).
- `scripts/tool-lint.py`: clean, exit 0.
- Full suite: 1693 passed, 5 failed — all relay-endpoint tests
  (test_line_relay_quota, test_line_reply_context,
  test_tg_relay_voice_reply) where `tests/dep_stubs.py`'s fabricated
  `starlette.Route` rejects the real 3-arg constructor — the ad-hoc gate
  venv (/tmp/ada-venv) lacks real starlette/uvicorn/numpy. Pre-existing
  env gap, unrelated; the real deploy venv has the deps.

## Measure (ada-ha-tony, before/after deploy)

- calls/day: `set_facial_expression` `function_call` lines in
  `~/.local/share/ada/call-logs/<day>-<session>.jsonl`; refused calls are
  identifiable by the "Expression unchanged" result text.
- in-tokens/turn: `usage.jsonl` `in` rows for `lane=ada-ha-tony`; each
  avoided call ≈ one ~28k resend.

## Trail

- `docs/ssot/jobs/ada/2026-10-26-expression-call-batching.yml`
- `docs/ssot/ssot.tool-surface.yml` comment updated (still lint-clean).

Env knobs if tighter diet is wanted later:
`ADA_EXPRESSION_MINUTE_BUDGET=2`, or `ADA_EXPRESSION_TURN_BUDGET=0` to
retire the tool from voice lanes entirely.
