# Dispatch outcome — ada-nest-trivial-gate (tg_relay adaptive-depth gate)

## What changed

A new **trivial-answer gate** (`backend/trivial_gate.py`) is the last cheap
stage of the TG relay cascade — silence detect → STT → `_is_trivial` noise
filter → **gate** → Ada turn — the NCM adaptive-depth principle applied to
the relay lane.

- **L0 rules** — anchored fullmatch intent patterns over normalized text
  (EN + TH): greeting, thanks, bye, time/date, identity ("who are you"),
  small talk ("how are you"), ack, kudos, emoji-only. Replies are canned or
  deterministic (time/date computed; greeting mirrors daypart). Rules only
  fire on a full match — "thanks can you open the gate" still reaches Ada.
- **L1 model** — optional `/v1/systemone` intent classifier
  (`ADA_TRIVIAL_LLM_URL`, falls back to `ADA_COMMAND_LLM_URL`). The model
  only picks a canned intent, never free-generates; deflects require
  conf ≥ `ADA_TRIVIAL_MIN_CONF` (0.75). 'time' is deliberately excluded
  from L1 criteria ("what time is it in Bangkok" wants a timezone answer).
  Unset URL = rules-only, still fully armed.
- **Hot-session suppression** — answer-shaped acks ("yes"/"ok"/"sure"/"👍")
  pass through when the peer has a live session active within
  `ADA_TRIVIAL_QUIET_S` (90s), so replies to Ada's own questions still
  reach her. Cold acks get "👍".
- **Wiring** (`backend/tg_relay.py`) — voice path: after the transcript
  echo, before session creation (deflected turns keep the echo so the user
  sees what STT heard); text path: after `/start`, skipped for forwarded
  messages. Replies go through `send_text` with the normal reply tag and
  `reply_to`.
- **Measurability** — every evaluation appends a corpus row
  (`ADA_TRIVIAL_CORPUS`, default `~/.local/share/ada/trivial-gate-corpus.jsonl`)
  and each decision logs the running deflection rate:
  `trivial_gate text: deflect intent=greeting layer=rules conf=1.00 ms=0 | deflected 3/10 (30%)`.

## Modes (`ADA_TRIVIAL_GATE`)

- `off` — disabled.
- `enforce` — **(default)** answers confident trivial turns locally.
- `shadow` — evaluates fire-and-forget, records `would-deflect` rows,
  never answers. Zero added latency for pre-promotion measurement.

## Env (documented in `.env.example` + tg_relay docstring)

`ADA_TRIVIAL_GATE`, `ADA_TRIVIAL_LLM_URL`, `ADA_TRIVIAL_MIN_CONF` (0.75),
`ADA_TRIVIAL_TIMEOUT_S` (2), `ADA_TRIVIAL_MAX_WORDS` (20),
`ADA_TRIVIAL_QUIET_S` (90), `ADA_TRIVIAL_CORPUS`.

## Verification

- `python3 -m pytest tests/test_trivial_gate.py` — **24 tests, all pass**
  (rules, modes, L1 thresholding, hot-session suppression, relay wiring
  for voice + text + forwarded + shadow).
- `python3 -m pytest tests/ -x -q` — **1614 passed, 7 skipped** (full suite).
- `python3 scripts/tool-lint.py` — clean (61 pre-existing warnings).
- `python3 -m py_compile` clean; standalone `import tg_relay` from
  `backend/` resolves trivial_gate via the fallback import (relay runs as
  a script on the hosts).
- Live logic smoke: "hi ada"→"Hey!" / "turn on the fan"→pass /
  "what time is it"→"It's 10:40 AM." / "yes" (hot)→pass /
  "thank you so much"→"You're welcome!" / "ok" (cold)→"👍" — all <5ms.

## Notes / limits

- No commit, push, or deploy — worktree only, as instructed.
- The idc02 model-shelf provisioning (a small CPU instruct lane next to
  ada-stt serving `/v1/systemone`) is a host-ops step outside the
  worktree; until then L1 rides `ADA_COMMAND_LLM_URL` or stays
  rules-only (rules cover every intent class the card names).
- LINE relay is the later scope — the module is lane-agnostic (neutral
  `ADA_TRIVIAL_*` envs, persona passed per call).
- SSOT trail: `docs/ssot/jobs/ada/2026-10-22-trivial-gate.yml`.

## Files

- `backend/trivial_gate.py` (new, ~380 lines)
- `backend/tg_relay.py` (import, `_gate_hot`, voice+text call sites, env docs)
- `.env.example` (ADA_TRIVIAL_* block)
- `tests/test_trivial_gate.py` (new, 24 tests)
- `docs/ssot/jobs/ada/2026-10-22-trivial-gate.yml` (new)

## One-paragraph summary

Implemented the trivial-answer gate for the TG relay's adaptive-depth
cascade: `backend/trivial_gate.py` answers greetings/acks/thanks/bye/
time-date/persona small talk locally via anchored L0 rules (EN+TH,
sub-ms) plus an optional L1 `/v1/systemone` intent classifier on the
model shelf (`ADA_TRIVIAL_LLM_URL`, falling back to
`ADA_COMMAND_LLM_URL`, confidence-floored at 0.75, classify-only — never
free-generates); `tg_relay.py` routes voice transcripts and text turns
through it before any Ada session is touched (forwarded content bypasses
the gate), hot-session suppression keeps answer-shaped acks ("yes"/"ok")
reaching Ada during live conversations, and every decision lands in a
corpus JSONL plus a running deflection-rate log line — verified with 24
new tests, the full 1614-test suite green, tool-lint clean, and a live
smoke showing deflects in <5ms; default `ADA_TRIVIAL_GATE=enforce` is
rules-only until an L1 URL is configured, the idc02 shelf lane is noted
as host-ops follow-up, SSOT trail at
`docs/ssot/jobs/ada/2026-10-22-trivial-gate.yml`, no commit/push/deploy
performed.
