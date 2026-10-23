# Dispatch outcome — ada-phantom-claim-gate (20261023-084031)

**Task**: kk-dev probes 2026-10-12 — (1) `chat_send photo:pick`
policy-DENIED but Ada told the user it was being sent; (2) a catalog
prime run returned `cms_read` ok then claimed delivery with ZERO
`chat_send` calls. Same class as `phantom_write_claim`, but the
detector was telemetry-only — the false claim reached the user's ears
unopposed. Card asks for provider-level handling: a tool result that is
an error/denial, or an expected send that never fired, must not leave a
success claim standing — deterministic post-check or forced re-voice —
verified via a scenario asserting `response_not_contains` on
success-claim phrases after a forced denial.

## Root cause — why "being sent" survived a denial

Three stacked holes:

1. **Vocabulary**: `_PHANTOM_CLAIM_RE` had write/board/screen verbs but
   no send/delivery vocabulary at all — "it's being sent", "on its way",
   "ส่งแล้ว" matched nothing.
2. **No pre-voice marker**: gate denials carry the `NOT EXECUTED` prefix,
   but a tool that ran and returned `ok:False` (or a denied call whose
   error text the model skimmed past) had nothing forcing honesty into
   the composition — the model papered over it.
3. **No remediation channel**: `phantom_write_claim` emits an ops event
   at turn end — AFTER the false audio already played. Nothing spoke a
   correction.

A fourth, pre-existing detector hole surfaced while testing: the claim
counters (`tool_calls/failed_results/backing_calls_this_turn`) reset at
EVERY `turn_complete`, including mid-task segment boundaries — so an ok
send in segment 1 could never back a "sent it" narrated in segment 2.
Honest sends were themselves flagging as phantoms.

## What changed — `backend/realtime_provider.py`

- **`_PHANTOM_CLAIM_RE`** — send/delivery claims (EN+TH):
  `sent/delivered/forwarded/pushed + object`, `being sent`,
  `on its/the way`, `sending it/that/…`; TH `ส่ง…แล้ว/เรียบร้อย`,
  `กำลังส่ง`. Promises ("I'll send it", "เดี๋ยวส่งให้") carry no done
  marker and stay out.
- **`_CLAIM_SUBJECT_RE`** — a done-verb on a non-Ada subject
  ("you sent it", "เขาส่งแล้ว") isn't Ada's claim.
- **`_RECALL_CLAIM_RE`** — past-time frames ("I sent it yesterday",
  เมื่อวาน/ก่อนหน้า) mark a claim as recall: `phantom_write_claim`
  telemetry still fires, but the re-voice stands down — correcting
  recall speech would narrate a falsehood of our own.
- **`speech_rule` injection (deterministic post-check)**: a
  claim-capable call (`_claim_backing_call`) returning `ok:False` gets
  `_CLAIM_FAILED_SPEECH_RULE` merged into the result blob — the honesty
  contract now travels WITH the result the model composes over, for
  gate denials AND tool-level failures. Read-only failures carry no
  admonition.
- **Forced re-voice (`_send_claim_correction`)**: at the FINAL turn
  boundary, a `_phantom_claim` hit with `backing_calls == 0` reopens
  the model turn (`send_client_content`, turn_complete=True) — Ada must
  say aloud, now, that it did not go through, name the real reason
  (the turn's failed results are handed over), or clarify the claim
  meant an earlier turn. Mid-task boundaries stand down (a pending call
  can still back the claim); `_revoice_in_flight` suppresses re-arm
  inside the correction turn (one per claim, no loop). Emits a new
  `phantom_claim_revoice` ops event.
- **Counter lifecycle fix**: the three claim-gate counters now reset
  only at the final boundary (`not tools_pending`) — same lifecycle as
  `turn_tool_results`. Backing spans the whole user turn.
- **Instructions**: DONE MEANS DONE gains the send clause on both
  variants — "sent"/"on its way" only when `chat_send` returned ok
  THIS turn.

## Verification

- `tests/test_provider_events.py` — new `PhantomClaimRevoiceTests`
  (10 cases, end-to-end `events()`): denied send → correction +
  `phantom_claim_revoice`; ok send → clean; zero-call delivery claim →
  correction; `cms_read`-only turn → correction (probe 2 repro);
  `speech_rule` on claim-capable failures only; recall-framed claim →
  telemetry but NO correction; correction turn can't re-arm (1 nudge,
  2 telemetry); honest failure report → clean. Plus regex coverage for
  the new vocabulary/guards. Note the fake sessions now emit the
  realistic mid-task `turn_complete` between tool dispatch and reply.
- `tests/scenarios-live/chat_send_denied_claim.yaml` — forced denial
  via the existing `?simulate_speaker=KK` hook (KK is secondary →
  `chat_send photo:pick` hits the secondary-speaker block
  deterministically). `response_not_contains` on success-claim phrases
  ("sent it", "been sent", "is being sent", "on its way", ส่งแล้ว,
  ส่งให้แล้ว, กำลังส่ง, …) + a follow-up honesty turn requiring
  denial/couldn't framing. Honest refusal-up-front also passes — both
  are correct outcomes.
- `pytest tests/` — 1633 passed, 7 skipped (full suite, ~3min).
- `scripts/tool-lint.py` — clean. `scripts/scenario-schema-check.py` —
  clean (198 files).

Trail: `docs/ssot/jobs/ada/2026-10-23-phantom-claim-gate.yml`.

## Live check post-deploy

- kk-dev probe replay: as a secondary speaker ask for the photo
  picker — either refusal or a spoken denial, never "sent". Ops feed
  shows `phantom_write_claim` + `phantom_claim_revoice` if she slips.
- Owner session: "send me the catalog" with LINE relay down — any
  claimed delivery over a failed/zero send gets an audible correction.

Not committed/pushed per dispatch rules — changes are in this worktree.
