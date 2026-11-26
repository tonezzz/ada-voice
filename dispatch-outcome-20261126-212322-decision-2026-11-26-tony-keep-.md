# Dispatch outcome — 2026-11-26 — deploy-ada.sh SERVICES: drop ada-dev, keep ada-kk-dev

Card: `deploy-ada-sh-services-resurrects-retire-c2346b` (ADA-1395)
Branch: `dispatch/20261126-212322-decision-2026-11-26-tony-keep-` (commit `c1eae8c`)

## What changed

- **Repo canon** `scripts/deploy-ada.sh`: `SERVICES` default dropped the
  `ada-dev` token; `ada-kk-dev` kept. One-line change, committed on the
  dispatch branch (not pushed, per dispatch rules).
- **idc03 installed copy** `~/.local/bin/deploy-ada.sh`: backed up to
  timestamped sibling `~/.local/bin/deploy-ada.sh.bak-20261126-214010`
  (`cp -p`), then edited in place — only line 33 (SERVICES) differs from
  the checkout canon. `bash -n` clean.

## What was found

- idc03's installed script was byte-identical to its checkout canon — the
  9970-byte dell-local-`main` version. The worktree canon (origin/main)
  is newer (11777 B, adds the 2b tools-snapshot block); the two ref lines
  have diverged 3/3 — pre-existing, flagged on the card.
- `ada-dev.service` is `active running` but `is-enabled=disabled`; its
  last start (ActiveEnterTimestamp `Thu 2026-11-26 18:24:39 +07`) lines
  up with the previous deploy — the "resurrect" pattern. It is still
  running; this change only stops deploys from restarting it.
- `ada-kk-dev.service` is the live kk CMS-only bench lane: healthy on
  `127.0.0.1:8009/api/health` (`lane: kk-dev`, 0 sessions).
- `kk-bench-env.py` lives in the **chaba** repo
  (`~/CascadeProjects/chaba/scripts/ada/kk-bench-env.py`), not ada-voice;
  bench profile `kk-cms-only` targets `ada-kk-dev.env@idc03`, restarts
  `ada-kk-dev.service`, health-checks :8009.
- From tony-dell, `ssh idc03` fails: the configured `ProxyJump tony-omen`
  is refused by omen's sshd ("channel 0 open failed administratively
  prohibited"). `ssh -o ProxyJump=none idc03` works (direct tailnet),
  and `ssh idc03` also works *from* idc03 and tony-omen.

## Verification (all run live on idc03)

- Restart-set expansion of the edited script: `ada-ha-tony ada-ai-kraut
  ada-tg-relay-kk ada-tg-relay-mn ada-kk-dev ada-kk ada-line-relay
  ada-tg-relay` — **no ada-dev**, all 8 units `active`.
- `ada-kk-dev` manageability: `systemctl --user restart
  ada-kk-dev.service` → healthy on :8009 in 11 s (dep-gate deps MDDB
  :11023 + notebooklm :3011 verified OK first); `kk-bench-env.py
  --profile kk-cms-only --check` → `OK … exit 0`.
- Drift-gate probe: `~/.local/bin/deploy-ada.sh` now exits rc=1 at gate 0
  with "deploy script drifted from canonical … — reinstall it" **before
  any mutation** — expected until canon catches up (below).

## How to verify / finish the convergence

1. Merge this dispatch branch (dispatcher close-out). Once the change is
   on the ref idc03 fetches (`ssh://tony-dell/.../ada-voice` → dell's
   local `main`), on idc03:
   `git -C ~/CascadeProjects/ada-voice fetch origin && git merge --ff-only origin/main`
2. If the drift gate still fires (canon gained other hunks, e.g. the 2b
   snapshot block), run the remedy it prints:
   `install -m 0755 scripts/deploy-ada.sh ~/.local/bin/deploy-ada.sh`
   — canon already lacks `ada-dev`, so the reinstall keeps the decision.
3. Proof the next deploy skipped ada-dev:
   `systemctl --user show ada-dev.service -p ActiveEnterTimestamp --value`
   must still read `Thu 2026-11-26 18:24:39 +07` after the deploy, while
   `ada-kk-dev`'s timestamp advances.

## Follow-ups noted (not done — out of scope)

- `ada-dev.service` still runs; if "retired" means stopped, a separate
  `systemctl --user stop/disable` decision is needed (unit is already
  disabled, so it won't return on boot — only on deploy restart, now
  removed).
- idc02 has the same installed `deploy-ada.sh` with `ada-dev`; staging
  only, untouched per card scope.
- `ada-kk-dev.service` unit comment still says "MIGRATED to tony-omen
  2026-11-18 … kept disabled+stopped on idc03" — stale; the lane is live
  on idc03:8009 as the declared bench target.
- dell local `main` ↔ GitHub `origin/main` divergence (3/3) means idc03
  deploys currently gate on code missing the `ada_capabilities` wiring.

## Trail

- Job record: `docs/ssot/jobs/ada/2026-11-26-deploy-ada-sh-services-drop-ada-dev.yml`

lessons:
- deploy-ada.sh self-drift gate compares installed copy to the checkout canon BEFORE the ff-merge — editing only ~/.local/bin/deploy-ada.sh blocks every deploy until canon catches up; mirror the change in the repo (dispatch branch) and expect one drift-gate pause.
- idc03's deploy origin is tony-dell's LOCAL checkout (ssh://tony-dell/...), not GitHub — canon changes must land on dell's local main ref to reach idc03.
- From tony-dell use `ssh -o ProxyJump=none idc03`; the configured ProxyJump tony-omen is refused (administratively prohibited). Plain `ssh idc03` works when run ON idc03 or from tony-omen — kk-bench-env.py --check runs fine on idc03 itself.
- kk-bench-env.py lives in the chaba repo (scripts/ada/), manages ada-kk-dev.env@idc03 and restarts ada-kk-dev.service — it does not depend on deploy-ada.sh SERVICES at all.
- `systemctl --user show <unit> -p ActiveEnterTimestamp` is the cheap proof that a deploy did/didn't restart a unit — record the baseline before changing SERVICES.
