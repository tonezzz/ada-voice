# Dispatch outcome — ada-tool-voice-request-well-formed-kanba-56ba9f (attempt 2)

## What happened

Attempt 1 (tony-macbook, 15:25) implemented the feature and pushed
`origin/dispatch/20261125-152559-implement-the-ada-tool-capabil`, but the
merge-sweep failed: the landing push tried to update `refs/heads/main` on a
non-bare checkout (`refusing to update checked out branch`), and the retry
then reported "branch head == base tip — nothing committed". The work was
never lost — it was sitting on the origin dispatch branch.

This attempt (idc02): cherry-picked the three commits from that branch
onto `dispatch/20261125-164138-implement-the-ada-tool-capabil` (clean
pick, no conflicts) and re-verified on this host.

## What changed (unchanged from attempt 1, now landed on this branch)

- `backend/tools.d/kanban.py` — `action='draft'` turns a dictated request
  (verbatim voice-to-text transcript) into a validated card spec with no
  board write; `action='file'` accepts `spec`/`verify`/`done_when`/`tags`/
  `column`/`card_action`/`queue` validated against the Chaba board card
  schema; `queue=true` arms dispatch behind the confirm gate.
- `backend/tool_guide.yml` — documents draft -> confirm -> file flow.
- `tests/fixtures/voice-kanban-request.txt` — recorded transcript fixture.
- `tests/test_kanban_voice_draft.py` — integration test: transcript ->
  draft -> file(confirmed, queue=true) on FakeBoard -> card lands
  `column=backlog`, `action.status=queued`.
- `tests/scenarios/kanban_voice_card.yaml` — same path in the offline
  scenario suite.
- `tests/test_kanban.py` — unit coverage for draft/file schema.
- `docs/ssot/jobs/ada/2026-11-25-kanban-voice-card-draft.yml` — job record.

## Verification (this host, idc02)

- `pytest tests/ -x -q` — **1995 passed, 7 skipped** using the shared
  deps venv `~/CascadeProjects/ada-voice/.venv` (this worktree has no
  .venv; plain `pytest` fails on `import httpx` — same trap as attempt 1).
- `python3 scripts/tool-lint.py` — clean.
- Branch is `origin/main` + 3 commits; not pushed, not deployed (per
  dispatch rules).

## How to verify / next step

Merge the dispatch branch via the normal sweep. Deploy is
`~/.local/bin/deploy-ada.sh` on idc03 by the operator. Post-deploy e2e on
ada-dev: dictate a request -> `kanban action='draft'` -> read spec back ->
file on confirm.

lessons:
- A failed merge-sweep "nothing committed" verdict can mean the work is safe on the pushed `origin/dispatch/*` branch — check `git log origin/main..<old-branch>` before re-implementing.
- Dispatch worktrees on idc02 have no `.venv`; use `~/CascadeProjects/ada-voice/.venv/bin/python -m pytest` (plain `pytest` fails on `import httpx`).
