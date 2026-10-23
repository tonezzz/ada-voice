# Dispatch outcome — tg-voice-out-lane (TG relay voice replies)

## What changed

The missing bridge between a completed Ada turn and a Telegram voice
bubble. Opted-in lanes now synthesize the reply with Gemini TTS and push
it through the relay's own loopback `/send` — the same `audio_b64`
contract `chat_send say=` rides, so wav→ogg→sendVoice stays one path.

- **`backend/tg_relay.py`**
  - `TgRelay._voice_reply(chat_id, reply, voice_inbound)` — gated on
    `TG_VOICE_REPLY`: unset/`0`/`off` = never (default); `voice` = only
    replies to voice/audio inbound; `1`/`all`/`on` = every completed
    turn. Scrubs the reply via `speech_sanitize.sanitize_speech`
    (truncates at `TG_VOICE_REPLY_MAX_CHARS`, default 2000 — vcast
    pattern so cut-off markup is dropped, not spoken), calls
    `tts.synth_pcm`, then POSTs `{chat_id, audio_b64(wav),
    audio_mime: audio/wav, duration_ms}` to `_send_url()`.
  - `TgRelay._send_url()` — derives `http://<TG_WEBHOOK_LISTEN>/send`
    (0.0.0.0/:: → 127.0.0.1) so each lane voices through its **own** bot;
    `TG_VOICE_SEND_URL` overrides. `ADA_TG_SEND_URL` is deliberately NOT
    honored — it's the tools' env and may name another lane's bot.
  - Wired after `send_text` in all three reply paths (voice-clip,
    photo, text) behind a `turn_ok` flag — failure sentinels
    ("session dropped") are never voiced; trivial-gate deflections stay
    local/cheap. Text lands first, the voice bubble follows a few
    seconds later. Every failure path is warn-and-continue.
  - **Parity fix**: the two duplicated `send` closures are now one
    `_send_endpoint()` mounted by both webhook and polling modes —
    polling `/send` previously rejected `audio_b64` with 400
    "nothing to send" despite claiming the same contract.
- **`backend/tts.py`** — dual-mode imports (`from backend import
  gemini_pool`/`voice_config` with sibling `import` fallback) so
  script-mode `python backend/tg_relay.py` can load it.
- **`.env.example`** — documented `TG_VOICE_REPLY`,
  `TG_VOICE_REPLY_MAX_CHARS`, `TG_VOICE_SEND_URL`.
- **`tests/test_tg_relay_voice_reply.py`** — 9 tests: gate modes,
  payload shape (RIFF wav b64, `duration_ms`, own-`/send` URL),
  voice-only mode, tts-fail/failed-turn/gate-deflection non-voicing,
  `/send` audio_b64→sendVoice, `_send_url` derivation.
- **`docs/ssot/jobs/ada/2026-10-23-tg-voice-out-lane.yml`** — SSOT trail.

## Result

`TG_VOICE_REPLY=1` (or `=voice`) on a lane → text reply lands first, a
native voice bubble in Ada's configured voice follows ~seconds later.
Unset lanes unchanged. Michael's lane needs the env set in its relay
unit plus `GEMINI_API_KEY*` reachable from the relay process
(`gemini_pool`), same as vcast.

## Verify

- `.venv/bin/python -m pytest tests/test_tg_relay_voice_reply.py -q` → 9 pass
- `.venv/bin/python -m pytest tests/ -q` → **1630 passed, 7 skipped**
  (worktree venv built with `uv pip` — host python has no ensurepip)
- `python3 scripts/tool-lint.py` → clean (61 pre-existing warnings)
- `cd backend && ../.venv/bin/python -c "import tg_relay, tts"` →
  script-mode import resolves both dual-import paths
- Live: set `TG_VOICE_REPLY=1` on a relay lane, send a message → text
  reply then voice bubble in the same chat.
