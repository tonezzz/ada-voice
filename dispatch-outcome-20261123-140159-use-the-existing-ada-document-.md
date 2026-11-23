# Dispatch outcome — ada-printer-diagnostics-safe-repair (ADA-1049)

Attempt 4, runner idc02 (dispatch 20261123-140159). Result: **done —
the approved queue retarget is executed and verified live; all gates
green; no Ada production deploy (idc02 staging only, per AGENTS.md).**

## Context

Attempt 3 (dispatch 105454, same host) built the whole surface —
`scripts/ada-cups-helper.py`, `backend/printer_client.py`,
`backend/tool_runner/printer.py`, `backend/printer_registry.yml`,
wiring, 25 tests, SSOT doc — and left exactly one gate open: the live
`HP_DeskJet_2700` retarget `.75 -> .92` waited on Tony's approval. The
merge of attempt 3's branch is present in this worktree; the zombie
restarts (113415/114935) died on provider rate-limits, not on code.

Tony answered **"proceed"** at 13:59 on board request
`ipp-identity-verified-2026-11-23-via-ton-f51f78`, so this session ran
the retarget.

## What changed this session

- **Live (tony-dell CUPS):** re-verified IPP identity at 192.168.2.92
  (serial `CN33BDZ0KS`, uuid `5ba42c21-ae68-7f6d-830e-600ee5b906fe`,
  idle/accepting, ink 70/40), then
  `ada-cups-helper.py set-queue-uri HP_DeskJet_2700
  ipp://192.168.2.92/ipp/print --expect-serial CN33BDZ0KS
  --expect-uuid ...b906fe`. The helper re-probed, matched, lpadmin
  applied — `device_uri_now` confirmed `.92`. Post-change `status`:
  all three queues probe the verified device; the default queue is
  idle + accepting with an empty job list.
- `backend/printer_registry.yml` — `HP_DeskJet_2700`
  `device_uri_observed` -> `ipp://192.168.2.92/ipp/print`, status ->
  `verified`; the `.75` stale_target note now records that the queue
  pointed there until the 2026-11-23 retarget (kept in stale_targets —
  it remains a wrong-host class of target).
- `docs/ssot/jobs/ada/2026-11-23-printer-diagnostics-safe-repair.yml` —
  step 5 records the approval + executed retarget; subtitle, scope,
  live_read, registry_eval, and followups updated to post-retarget
  truth.

## Card requirements — status

- Read-only status (identity, reachability, state, accepting, job
  ids/states, ink, source host) — `printer action=status|jobs|verify`.
- CUPS-host vs printer-internal jobs — `origin: cups|printer-internal`
  labels; repair refuses internal jobs.
- Repair allowlist = release|retry|cancel ONE verified job; the
  existing same-session confirm gate (confirmed=true / bound
  confirm_token fingerprinted to exact args); post-act re-read + audit
  block + `printer_repair` session event + change-request log.
- No arbitrary shell/sudo (argv-only helper), no cancel-all (job-id
  regex + no -a path), no test page/reprint op exists at all.
- Registry + queue target updated only after IPP identity
  verification — done, with Tony's explicit proceed.

## Gates

- `pytest tests/test_printer.py` — 25 passed (staging venv
  `~/CascadeProjects/ada-voice/.venv`; worktree has none).
- `python3 scripts/tool-lint.py` — clean (71 declared, cap 115).
- `pytest tests/ -x -q` — **1881 passed, 7 skipped** (4m01s).
- Live helper smoke post-retarget — all queues green against the
  verified device.
- Uncommitted (dispatch checkpoint convention): only the two files
  listed above. No push, no deploy — idc03 deploy via
  `scripts/deploy-ada.sh` remains Tony's call once staging is
  accepted.

## How to verify

1. `cd <worktree>`;
   `~/CascadeProjects/ada-voice/.venv/bin/python -m pytest
   tests/test_printer.py -x -q`.
2. `python3 scripts/tool-lint.py`.
3. Live: `ssh tony-dell 'lpstat -v HP_DeskJet_2700'` ->
   `ipp://192.168.2.92/ipp/print`; and
   `ssh tony-dell 'python3 ~/bin/ada-cups-helper.py status'` -> all
   queues probe serial CN33BDZ0KS / uuid ...b906fe.

lessons:
- Dispatch worktrees have no .venv — borrow the staging checkout's
  (`~/CascadeProjects/ada-voice/.venv/bin/python -m pytest`); system
  python lacks httpx.
- merge-sweep "verified gate failed / unit did not exit 0" can mean
  the session died (provider rate-limit), not that tests failed —
  check card comms for the rate-limit signature before assuming a
  code regression.
- Prior attempt's merged work + a board request answered mid-queue
  ("proceed") means the NEXT dispatch may inherit an authorized live
  change — always check card comms/answers before planning.
- `set-queue-uri` needs no sudo on tony-dell (lpadmin group covers
  it) — the helper's identity-expectation flags are the real gate.
