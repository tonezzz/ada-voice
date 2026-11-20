# dispatch-outcome: ada write-fidelity drop-parity (ADA-1300)

Card: `ada-write-fidelity-drop-parity` — runner tony-omen — 2026-11-20
Worktree: ada-voice dispatch branch `dispatch/20261120-130531-observed-on-tony-dell-chaba-te` (HEAD 6a39e3a, 36 commits ahead of canonical edb7c83).

## What was found

The chaba-side failure `tests/ada/test_write_fidelity_drop.py::test_patch_state_vs_checkout`
("diverged" after 269 passes + 1 skip) is **context rot, not guard drift**:

- The write-fidelity guard landed upstream in **04154f6** (2026-10-17,
  ADA-0732): `_fidelity_tokens`/`_write_fidelity_problems` + `write_fidelity`
  ops event in `backend/realtime_provider.py`, `stored_verified`/`stored_excerpt`/
  `stored_sha256` read-back in `backend/tool_runner/cms.py`,
  `tests/test_write_fidelity.py` (new, 17 tests), scenario yaml update.
- All guard code is **byte-identical** from 04154f6 → canonical edb7c83 →
  current HEAD 6a39e3a. `cms.py` has zero commits in the window; the two
  provider functions diff clean; the call-site code is unchanged.
- Root cause: **bcd6530** (2026-10-23, phantom-claim checkpoint) wrapped the
  `tool_calls_this_turn = 0 / failed_results_this_turn = 0` resets in
  `if not tools_pending:` — a block that sits immediately after the
  `_fidelity_flags` loop, inside the staged artifact's context window. Any
  artifact cut before that commit therefore matches neither pre-image nor
  post-image context at edb7c83 → the parity check reports "diverged".
- Canonical functional fidelity tests: `python3 -m pytest
  tests/test_write_fidelity.py -v` → **17/17 pass** (test file identical at
  edb7c83 and HEAD).

## What was delivered

- `reports/2026-11-20-write-fidelity-canonical-edb7c83.patch` — the upstream
  guard diff recontextualized to canonical edb7c83 (10 hunks, 4 files, same
  file set as 04154f6). Built by reverse-applying `git show 04154f6` onto the
  canonical files (fuzz ≤3; claim-gate context verified preserved), then
  diffing pre-image vs canonical.
- `docs/ssot/jobs/ada/2026-11-20-write-fidelity-drop-parity.yml` — full
  provenance, drift analysis, verification commands, and the chaba-side
  handoff recipe.

## Verification performed

- `git apply --check` of the refreshed patch against the synthetic pre-guard
  tree: clean; post-apply all four files **byte-identical** to
  `git show edb7c83:<file>`.
- `git apply -R --check` against canonical edb7c83 content: **passes** — the
  patch context is exactly canonical, so the parity check can classify the
  drop as integrated. Forward apply fails as expected (guard already
  upstream).
- Patch content audited: pure additions except the same 3 scenario-yaml line
  swaps 04154f6 made; no stray edits to `tools_pending`/claim-gate logic.

## Constraints respected

No hooks disabled, `_drops`/`drop_guard_ok` untouched, no tests skipped or
edited, no runtime guard changes, no push, no commits to main. Only two new
files added (patch + job record) — untracked on the dispatch branch.

## Handoff / open item

The staged artifact lives in chaba worktree `chaba-knb1257` — outside this
dispatch's reach. Board request `refreshed-context-only-artifact-is-ready-ae54ec`
asks Tony whether a chaba-side dispatch should perform the swap + re-run the
parity test to unblock the seven SSOT fixes for PR #138, or he'll swap it by
hand. Card comms carry the full finding summary.

## How to verify

```
python3 -m pytest tests/test_write_fidelity.py -v        # 17 passed
git apply -R --check reports/2026-11-20-write-fidelity-canonical-edb7c83.patch   # in a canonical-edb7c83 tree
```

lessons:
- Drop-parity "diverged" on ada artifacts is usually context rot in adjacent code, not guard drift — diff the guard regions by revision before assuming the artifact itself is wrong.
- A context-only artifact refresh is reproducible ada-side: reverse-apply the upstream guard commit onto canonical files (`patch -p1 -R --fuzz=3`), then diff pre-image vs canonical; verify with `git apply -R --check` against canonical content.
- bcd6530-style dispatch checkpoints that wrap adjacent statements (`if not tools_pending:` around counter resets) rot patch context even when they never touch the guarded code.
