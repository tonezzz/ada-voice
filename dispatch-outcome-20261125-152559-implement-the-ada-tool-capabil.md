# Dispatch outcome — ada-tool-voice-request-well-formed-kanba-56ba9f

## What changed

Implemented the voice-request -> kanban-card-spec capability in
`backend/tools.d/kanban.py` (ada-voice repo, branch
`dispatch/20261125-152559-implement-the-ada-tool-capabil`, commits
0839548 + 4a54542; not pushed, not deployed):

- **`action='draft'`** — takes `request=` (the verbatim voice-to-text
  transcript) and returns a validated card spec with NO board write:
  title derived from the first sentence, goal-first `spec` template,
  `verify`/`done_when` default, `tags=[voice-draft]`,
  `column=backlog`, `priority=medium`, and a suggested
  `card_action={type:dispatch, repo}` when the text names a known
  whitelist repo (chaba/ada-voice/sunsynk-card). Secondary-safe.
- **`action='file'` extended** — now accepts `spec`, `brief`, `verify`,
  `metric`, `report`, `done_when` (alias -> `verify`), `tags` (<=10),
  `column` (validated against the five standard board columns),
  caller `id=`, `card_action` dispatch envelope, and `queue=true`.
  Everything is validated against a mirror of the Chaba board card
  schema (`_CARD_ACTION_KEYS` mirrors board-api ACTION_KEYS /
  `opencode/ada_mcp.py _ENVELOPE_ACTION_KEYS`); malformed envelopes,
  bad columns, bad priority/tags are refused before any HTTP.
  `queue=true` arms dispatch rails on creation behind the standard
  confirm gate (`confirmed=true` / `confirm_token`) — matching the
  card spec's "POSTs /card column=backlog, NOT auto-queue" intent.
- **`_build_card_body()`** is the single assemble+validate path shared
  by draft and file.
- `backend/tool_guide.yml` kanban entry documents the
  draft -> confirm -> file flow.

## Done-condition result

Passing integration test where a recorded voice input generates a
correctly formatted card JSON in the board queue:

- `tests/fixtures/voice-kanban-request.txt` — the recorded transcript.
- `tests/test_kanban_voice_draft.py` — drives draft -> file
  (confirmed, queue=true) against the scenario engine's `FakeBoard`
  (the board-api contract stand-in) and asserts the landed card:
  `column=backlog`, `action.status=queued`, spec/verify/envelope
  intact, POST body schema-shaped.
- `tests/scenarios/kanban_voice_card.yaml` — same path in the offline
  scenario suite (auto-run by `tests/test_scenarios.py`).
- `tests/test_kanban.py` — 14 new unit tests (DraftTest, FileSpecTest).

## Verification

- `python3 scripts/tool-lint.py` — clean.
- `pytest tests/ -x -q` — **1998 passed, 4 skipped** (run with the
  borrowed deps venv `/home/tony/CascadeProjects/ada-voice-wt-wiki-confirm-flag/.venv`
  — this host has no project .venv).
- Commits are on the dispatch branch only; no push, no deploy (per
  dispatch rules — deploy is `deploy-ada.sh` on idc03 by the operator).

## How to verify / next step

Post-deploy e2e on ada-dev: dictate a request -> Ada calls
`kanban action='draft'`, reads the spec back, files on Tony's yes;
card lands in backlog with spec/verify/action envelope. Job record:
`docs/ssot/jobs/ada/2026-11-25-kanban-voice-card-draft.yml`.

lessons:
- tool-lint enforces DECLARATION description <= 4 lines — keep the
  blurb short, prose goes in tool_guide.yml.
- FakeBoard records POST bodies by shallow copy — board-added fields
  (action.status) leak into `audit: board` call logs; allow them in
  assertions.
- tony-macbook has no project .venv; borrowing another ada-voice
  worktree's .venv python works for pytest (deps only, read-only).
