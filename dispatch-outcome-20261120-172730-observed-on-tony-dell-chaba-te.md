# Dispatch outcome — ada-write-fidelity-drop-parity (attempt 2)

## What this run found

Attempt 1 (dispatch/20261120-130531, merged to main as 0d31fe2) already
delivered the owner-approved context-only artifact refresh:
`reports/2026-11-20-write-fidelity-canonical-edb7c83.patch` plus the job
record `docs/ssot/jobs/ada/2026-11-20-write-fidelity-drop-parity.yml`.
This run re-verified everything at current HEAD 7048cba and found the
artifact still fully applicable — the divergence the chaba test reports
remains context rot from bcd6530, not guard drift.

## Verification performed (all green)

- `git apply -R --check` on the refreshed patch: PASS against the four
  files extracted from canonical `edb7c83`, AND PASS against worktree
  HEAD — post-edb7c83 drift (80777c4, f38ea04; 13 lines in
  `backend/realtime_provider.py`) sits outside every hunk's context.
- Full round-trip on a scratch canonical tree: reverse-apply then
  forward-apply reproduces all 4 files byte-identical to
  `git show edb7c83:<file>` (realtime_provider.py, cms.py,
  test_write_fidelity.py, kk_cms_only_seeded_recall.yaml).
- `python3 -m pytest tests/test_write_fidelity.py -v`: 17/17 pass
  (0.85s, system python3 3.14.4 — no .venv in this worktree).
- Guard markers intact at HEAD: `_fidelity_tokens`,
  `_write_fidelity_problems`, `_fidelity_flags` call site, ops event
  `"write_fidelity"` (realtime_provider.py); `stored_verified` /
  `stored_excerpt` / `stored_sha256` (cms.py).

## Changes made this session

- `docs/ssot/jobs/ada/2026-11-20-write-fidelity-drop-parity.yml`:
  appended `reverified_20261120_172730` section with the checks above,
  and fixed pre-existing invalid YAML (plain scalars containing `: `
  in the `findings.guard_provenance.files` list — the file did not
  parse at HEAD; now `yaml.safe_load` clean).

No runtime guard code, hooks, tests, `_drops`/`drop_guard_ok` semantics,
or patch content were touched. No commit to main, no push, no deploy.

## Still blocked (owner decision pending)

Board request `refreshed-context-only-artifact-is-ready-ae54ec` remains
OPEN (options: proceed / hold). The staged `write-fidelity.patch` lives
in chaba worktree `chaba-knb1257`, which is outside this ada-voice
dispatch's reach. Until Tony picks who performs the swap, the seven
SSOT fixes for PR #138 stay blocked. Handoff recipe for the swap is in
the job yml (`handoff.to_chaba_side` + `regeneration_recipe`).

## How to verify

```
git apply -R --check reports/2026-11-20-write-fidelity-canonical-edb7c83.patch
python3 -m pytest tests/test_write_fidelity.py -v
python3 -c "import yaml; yaml.safe_load(open('docs/ssot/jobs/ada/2026-11-20-write-fidelity-drop-parity.yml'))"
```

lessons:
- Job-record YAML: `(+147: ...` plain scalars with `: ` inside list items break parsing — quote or fold them; always yaml.safe_load-check SSOT yml before writing it off.
- The refreshed write-fidelity artifact survived main moving past edb7c83 — drift outside hunk context doesn't rot a context-only patch; verify with `git apply -R --check` at BOTH canonical and HEAD.
- No .venv in dispatch worktrees — system python3 (3.14, pytest 9) + conftest dep_stubs runs the suite fine.
