# Dispatch outcome — ada-capabilities-verb-wiring (attempt 2)

Card: `ada-capabilities-verb-wiring` (chaba board, ref ADA-0020).
Repo: ada-voice. Contract: chaba `docs/kb/ada-tools-verb-contract.md`
(schema 1). Branch: `dispatch/20261126-192654-repo-work-ada-voice-wire-the-a`.

## What happened

Attempt 1 (2026-11-24, tony-macbook) implemented the full contract and
pushed `dispatch/20261124-212337-…` (commit cb7a0351), but the merge
gate failed closed: **"pytest unavailable — no test evidence"**
(OPS-1191 host-capability gate) and the merge-sweep then hit a
non-bare-repo push guard. The code itself was never faulted. This
attempt ran on tony-dell, which has a working venv + pytest.

## What changed (commit 1e11f11, cherry-pick of cb7a0351 — clean)

- `backend/tools.d/ada_capabilities.py` (new, 472 lines) — mtime-keyed
  snapshot loader (`ADA_TOOLS_SNAPSHOT` override; default
  `backend/data/ada-tools-snapshot.json`) + deterministic template:
  `I have N capabilities — M core + K add-on tools — as of <head_date>.`
  + 10 grouped areas + "Ask me about a specific tool or area for
  details." `topic=` detail mode resolves tool → absorbed-alias
  redirect → group label → merge family → unique substring → honest
  `ok:false`. Canonical names only — `aliases` keys filtered from
  `tools[]` surface; multi-layer rows dedupe builtin>dropin>guest.
  Failure modes per contract: missing/corrupt → `ok:false`; `behind>0`
  or >30d → stale caveat + warning; `schema!=1` → refuse.
- `backend/tools.d/manifest.yml` — `policy: read`,
  `secondary_allowed: true`, `timeout_s: 5`.
- `backend/tool_guide.yml` — error-path entry (quote `answer`, relay
  caveat, never fabricate).
- `scripts/deploy-ada.sh` — step 2b after ff-merge: copies chaba
  `docs/kb/ada-tools-commands.json` → `backend/data/ada-tools-snapshot.json`
  (mkstemp+os.replace, schema-1 validated, warn-not-block; `CHABA_REPO`
  override). `backend/data/` already gitignored.
- `tests/test_ada_capabilities.py` — 14 tests on fixture snapshot.
- `tests/scenarios-live/capabilities.yaml` — tier smoke,
  `needs_tools: [ada_capabilities]` — leaves coverage_debt.
- `docs/kb/ada-tools-snapshot.md` — transport runbook.
- `docs/ssot/jobs/ada/2026-11-26-capabilities-verb-wiring.yml` — this
  job's SSOT trail.

## Contract re-verification (this attempt)

Pulled the live contract + producer output via `gh api`
(tonezzz/chaba): snapshot top-level keys, `tools[]` field set,
`aliases{}`, `families{canonical,absorbed,status,merge_card,scenario}`,
`source.{head,head_date,behind}`, `schema:1` — all match the consumer.
The real snapshot carries an extra `drift` key — ignored per contract's
tolerate-unknown-keys rule.

## Gates (all green on tony-dell)

- `python3 scripts/tool-lint.py` → clean; surface 72 declared
  (38 builtin + 34 tools.d), cap 115, **66 scenario-covered**
  (capabilities.yaml counts; was 65).
- `python3 scripts/scenario-schema-check.py` → 212 files, 0 violations.
- `.venv`-borrowed pytest (`~/CascadeProjects/ada-voice/.venv`,
  py3.14.4, pytest 9.1.1): `pytest tests/ -x -q` →
  **2032 passed, 4 skipped, 31 subtests** (214s).
- Live-fire on the real chaba snapshot (copied into gitignored
  `backend/data/`): overview **3.7 ms cold / 0.68 ms warm** —
  "I have 68 capabilities — 37 core + 31 add-on tools — as of
  2026-11-19." + stale caveat (7 commits behind). Detail modes verified:
  tool (`kanban`), group (`home control`), alias redirect
  (`cctv_snapshot`→`ada_camera_snapshot`), unknown → honest `ok:false`.
- `bash -n scripts/deploy-ada.sh` clean; step 2b sits after ff-merge,
  before the mass-delete gate.

## How to verify

- `git show 1e11f11 --stat` — the full diff.
- `python3 scripts/tool-lint.py && python3 scripts/scenario-schema-check.py`
- `.venv/bin/python -m pytest tests/test_ada_capabilities.py -v`
- Post-deploy on idc03: deploy-ada.sh ships the snapshot; ask Ada
  "what can you do" — answer cites `head_date` and stale caveat.

## Notes for the merger

- The snapshot JSON itself is NOT committed (gitignored runtime
  artifact); first deploy after merge populates it. Until then the
  tool answers `ok:false` honestly — by design.
- chaba's committed snapshot was 7 commits behind ada-voice main at
  verification time; the producer reruns on decl change, deploy ships
  whatever chaba has committed.

lessons:
- Prior-attempt recovery works: `git fetch origin <dispatch-branch>` +
  `git cherry-pick <sha>` lands reviewed work without re-doing it;
  always re-run gates on the new base.
- "pytest unavailable — no test evidence" is a HOST gate (OPS-1191),
  not a code failure — check the claiming host has a venv/pytest before
  dispatching merge-heavy cards there.
- `gh api repos/<org>/<repo>/contents/<path>` reads private-repo files
  without leaving the worktree — useful for cross-repo contracts.
- Borrowing `~/CascadeProjects/ada-voice/.venv/bin/python` for pytest
  works when the dispatch worktree has no venv (read-only exec).
