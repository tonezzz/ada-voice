# dispatch outcome — 20261127-083400 investigate-the-failure-of-ada

Card: `timer-alert-idc02-ada-ocb-smoke` — investigate `ada-ocb-smoke.service`
failure on idc02 + redeploy runner-agent from chaba to idc02.

## Root cause (smoke service)

`ada-ocb-smoke.service` failed every hour with exit-code. Journal shows the
cascade starts at `find_card ADA-0172`: the fixture card ref has 0 live hits,
so `bridge.find_card()` falls back to fuzzy matching and now matches ~160
cards -> `LookupError`. `ada_context_pack` ("card snapshot missing") and
`ada_preflight` fail downstream for the same reason.

Card comms showed this exact fixture was already hot-fixed twice (11-25,
11-26) by editing `~/CascadeProjects/ada-voice/scripts/ocb-smoke.py` on idc02
(ADA-0172 -> ADA-1110 + per-check latency budgets) — but the edits were never
committed and were reverted, so the service regressed. Third recurrence.

## What changed

- **ada-voice** `scripts/ocb-smoke.py` — committed `02e8365` on dispatch
  branch `dispatch/20261127-083400-investigate-the-failure-of-ada`: ported the
  proven `.bak-retry-20261126-175859` fix verbatim (byte-identical): fixture
  repointed to ADA-1110 (`ada-smoke-regressions-20261107`, ref=ADA-1110) in
  find_card / context_pack / preflight, and `_check()` gained a per-check
  `limit` override — find_card and context_pack get 15s (MDDB fan-out reads
  run ~7.7–8.5s on idc02 vs the global 8s budget).
- **idc02** `~/CascadeProjects/ada-voice` — ff-pulled to origin/main
  (0a5dbd8, was 8 behind) and `scripts/ocb-smoke.py` overwritten with the
  committed bytes as a hotfix (shows ` M` until the dispatch merge lands,
  then self-cleans).
- **idc02** `~/.local/bin/runner-agent` — re-installed via
  `install -m 0755` from `~/CascadeProjects/chaba/scripts/board/runner-agent.py`.
  sha256 `38d84bc073da…` == origin/master file == manifest pin.

## runner-agent drift — resolved upstream mid-session

Installed copy already matched the chaba repo file, but the
`runner-tooling.manifest.json` pin (`4ed3afdb`) did not match file sha
(`38d84bc`) because chaba commit `5df508399d` changed runner-agent.py without
bumping the pin — that's why `sync-runner-tools` warned "source/manifest
mismatch, skipped" and fleet-health flagged tool-pin drift on every host.
While investigating, chaba `ed66bfb178` (08:38 +07) landed on origin/master
bumping the pin to `38d84bc` — contract restored; drift alerts clear on next
pin read. No chaba-side action needed.

## Verification

- `python3 scripts/ocb-smoke.py --dry-run` (worktree): all 6 checks PASS.
- `systemctl --user start ada-ocb-smoke.service` on idc02: `Result=success`,
  all 6 PASS, `benchmark/ocb-smoke-20261127-084720` written to
  ada-ha-scenario-reports. Next hourly timer tick (09:00 +07) uses the same
  fixed file.
- `sha256sum ~/.local/bin/runner-agent` = `38d84bc073da…` = pin.
- Job trail: `docs/ssot/jobs/ada/2026-11-27-ocb-smoke-fixture-runner-agent-idc02.yml`

## Follow-ups

- After the dispatch branch merges to ada-voice main, idc02's checkout needs
  one `git pull --ff-only` to clear the ` M scripts/ocb-smoke.py` hotfix
  (bytes will be identical, so it self-cleans).
- Fixture rot will recur whenever a pinned card ref dies — consider picking
  the smoke fixture from a stable "smoke fixture" card or making find_card
  assert on a live-ref allowlist.

lessons:
- Local hotfixes to tracked files on job checkouts get silently reverted —
  commit fixture/tooling fixes upstream; a `.bak` in the checkout is not a
  fix (this card recurred 3x on the same uncommitted edit).
- runner-tooling manifest pins are enforced against origin/master, not the
  host checkout — a tool commit without a same-commit pin bump flags
  tool-pin drift fleet-wide within the hour.
- `find_card`'s fuzzy fallback turns a dead card ref into a LookupError
  listing hundreds of matches; smoke fixtures must point at live refs.
- idc02 board/MDDB reads run ~8s — global 8s check budgets are marginal;
  per-check limits (15s) are the established pattern for fan-out tools.
