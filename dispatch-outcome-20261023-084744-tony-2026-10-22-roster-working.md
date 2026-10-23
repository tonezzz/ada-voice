# Dispatch outcome — webchat-history-endpoint (roster working)

Card: keep web-chat history across reconnects/devices, server-side.
Thread key = chat_id (`web-<member>`), surface-agnostic.

## What changed

- `backend/conversation_memory.py` — new per-chat store:
  `webchat_history_append` / `webchat_history` over
  `ADA_WEBCHAT_HISTORY_DIR` (default `~/.local/share/ada/webchat/<chat_id>.jsonl`),
  rolling `ADA_WEBCHAT_HISTORY_KEEP=500` turns/chat (append-only until
  KEEP+50, then rewrite to KEEP; per-file counts re-sync from disk so a
  restart trims correctly). Local-only, like the transcript archive.
  `ConversationMemory.chat_id` set by `/ws`; `_channel_append` journals
  `channel=="web"` turns into the chat_id's JSONL — the single funnel
  every user/assistant turn crosses. no_persist sessions and mirrored
  sibling turns never write.
- `pwa_server.py`
  - `conversation.chat_id = chat_id` bound at `/ws` connect.
  - ws `{type:"history_request", max_turns}` → `{type:"history",
    chat_id, turns:[{role,text,ts,image_url?,video_url?}]}` — only the
    session's own bound chat_id is served (a member cannot name another
    thread).
  - `webchat_deliver` journals each proactive push as an assistant turn
    (text + media urls) — live or parked alike, so pushes survive remounts.
  - New `GET /api/chat/history?chat_id=&max_turns=` (default 100) —
    `_require_api_key` like `/api/chat/transcript`; ordered turns.
- `pwa/cards/ada-webchat-card.js`
  - First `ready` of a mount sends `history_request` (200 turns); live
    events buffer until the `history` frame or a 2s timeout (older
    lanes), so backfill renders ahead of new frames.
  - Backfill renders user → "mine", assistant → "ada" (+ media urls).
  - Parked pushes that also flushed this connect are deduped by
    text|image|video signature; a `history` frame late past the timeout
    is dropped; provider-reconnect `ready` never re-backfills.
  - Roster needs no change — the card backfills itself.

## Verification

- `tests/test_channel_history.py` +14 tests (store round-trip/order,
  per-chat_id isolation, max_turns tail, media-only, bad-line skip,
  KEEP rollover + restart re-sync, journal hook + exclusions).
- `tests/test_webchat.py` +3 (endpoint ordered turns + 422; deliver
  journals push).
- `/tmp/ada-test-venv/bin/python -m pytest tests/ -x -q` — **1652
  passed, 7 skipped** (scratch venv with fastapi/httpx/pyyaml/pillow/
  qrcode/websockets/numpy/uvicorn; the repo gate venv has all deps).
- `python3 scripts/tool-lint.py` — clean (61 pre-existing warnings).
- `node --check pwa/cards/ada-webchat-card.js` — ok.
- `python3 -m py_compile pwa_server.py backend/conversation_memory.py` — ok.

## Notes / edges

- REST endpoint is key-gated but not per-member-scoped — any valid key
  reads any chat_id (same bar as `/api/chat/transcript` which already
  exposes all live turns). Tighten to `web-<caller>` matching if wanted.
- Turns missed during a mid-mount ws gap are not re-synced (`_histDone`
  per mount); parked pushes still flush. A reload covers it.
- Card JS verified by inspection only — no browser on runner; worth
  eyeballing the first live backfill on boards.
- Deploy via `~/.local/bin/deploy-ada.sh` on idc03 (not run here).
  Done-when check after deploy: reload roster → prior turns per
  contact; second webui on same chat_id shows the same thread.

Trail: `docs/ssot/jobs/ada/2026-10-23-webchat-history-endpoint.yml`,
`docs/ssot/ssot.channels.yml` (web history stanza), `.env.example`.
