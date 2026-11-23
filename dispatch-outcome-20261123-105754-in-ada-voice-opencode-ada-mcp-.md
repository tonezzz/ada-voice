# dispatch outcome — ada-ocb-bridge-version (attempt 5, idc02)

Card: `ada-ocb-bridge-version` — BRIDGE_VERSION in `opencode/ada_mcp.py`,
`bridge` envelope on every tool response, `ada_bridge_info` tool,
`bridge_version` in calls.jsonl.

## What happened this run

This dispatch is a **duplicate retry**: Tony retried at 10:52 while
attempt 4 (`devin-task-20261123-105125`, tony-omen) was still running;
idc02 claimed and spawned this session at 10:57. Attempt 4 had already
completed and pushed `361723f` on `dispatch/20261123-105125`.

History:

- 11-18: base feature merged to main via `aa31f29` / `83c8098`
  (BRIDGE_VERSION, envelope, `ada_bridge_info`, calls.jsonl audit,
  startup event + stderr line).
- 11-20 (attempt 3, tony-omen): found real gap — `git describe --dirty`
  ignores untracked files, so a loose `ada_mcp.py` dropped inside a
  checkout reported the **identical** `git-<sha>` as canonical, defeating
  stale-copy detection. Fixed by appending `+copy-<sha256(file)[:10]>`
  when `git ls-files --error-unmatch` shows the file untracked. That run
  died on a provider rate limit before pushing.
- 11-23 (attempt 4, tony-omen): re-applied the fix as `361723f` on
  `dispatch/20261123-105125`, pushed, verified on a healthy host.
- 11-23 (this run, idc02): cherry-picked `361723f` verbatim as
  `b1059fc` onto `dispatch/20261123-105754` so the fix lands whichever
  sibling branch merge-sweep picks (identical content, conflict-free).

## Feature state (on this branch)

`opencode/ada_mcp.py`:

- `BRIDGE_VERSION` resolved at import: `ADA_BRIDGE_VERSION` env >
  `opencode/VERSION` file > `git describe --always --dirty --tags`
  (`+copy-<hash>` appended when the file is untracked in the repo git
  resolved) > `dev-<content-hash>` fallback.
- `_with_bridge_envelope()` wraps every audited tool response with
  top-level `{"bridge": {"version": BRIDGE_VERSION, "tools_count": N}}`.
- `ada_bridge_info()` tool reports version, full tool list, feature
  flags, audit path, workspace.
- `calls.jsonl` gets a `startup` event (version, tools_count, pid, cwd)
  plus `bridge_version` on every per-call entry; stderr prints
  `ada-control bridge <version> starting` at MCP boot.

## Verification (this host, this run)

- `tests/test_opencode_ada_mcp_bridge_version.py`: **9/9 pass**, three
  consecutive runs (8.9–29.6s each under load 18–24).
- All ada_mcp suites: **81/81 pass** (envelope, board_snapshots,
  context_pack, find_card, selection_gate, ssot_get, ssot_search).
- `python3 scripts/tool-lint.py`: clean.
- Live module probe (mcp stub — idc02 has no `mcp` wheel; real stdio
  probe evidence is in `reports/2026-11-23-ada-ocb-bridge-version-copy-tag.md`
  from tony-omen). Three copies, three distinct versions, all through
  the real audited call path with envelope + per-call JSONL:

  | scenario | version |
  |---|---|
  | canonical tracked copy (this worktree) | `git-b1059fc` |
  | untracked copy inside scratch git repo | `git-06d1566+copy-3cd10becaa` |
  | copy outside any repo (+`# stale` byte) | `dev-aeb2ce12b6` |

- **OpenChamber session evidence — idc02 live**
  `~/.local/share/ada-control/calls.jsonl`: 8 distinct `bridge_version`
  values serving real sessions (`git-6a39e3a` ×964 from the idc02
  staging checkout, `git-7c5ba50` ×205, `git-6b1fe35` ×126,
  `git-36828b1` ×98, `git-aa31f29-dirty` ×18 from a dispatch scratch
  copy, `git-964c5d2` ×8, one legacy raw-sha, `None` ×108 pre-feature).
  Startup events recorded per spawned MCP child — including children
  whose cwd is *this* dispatch worktree (10:58:02) and a sibling
  dispatch worktree (10:59:41), each reporting the version of the copy
  that actually exec'd. That is exactly the per-session/per-directory
  attribution the card wanted.

## One flake observed (environmental, not code)

At ~11:03, under load avg 26 on 4 cores, `git init`/`git describe`
subprocesses in the untracked-copy test timed out (10s) and were SIGKILLed
— the resolver then correctly degraded to `dev-<content-hash>`, which is
still unique per copy (fail-safe direction). Three immediate retries all
passed. No code change needed; noted so the next run doesn't misread a
red test as a regression.

## Deliverables

- `b1059fc` (cherry-pick of `361723f`) on
  `dispatch/20261123-105754-in-ada-voice-opencode-ada-mcp-`:
  `opencode/ada_mcp.py` fix + `tests/test_opencode_ada_mcp_bridge_version.py`
  + attempt-4 artifacts (`reports/2026-11-23-ada-ocb-bridge-version-copy-tag.md`,
  `docs/ssot/jobs/ada/2026-11-23-bridge-version-copy-tag.yml`).
- Sibling branch `dispatch/20261123-105125` carries identical content —
  merge either; both converge.

## How to verify after merge

1. `.venv/bin/python -m pytest tests/test_opencode_ada_mcp_bridge_version.py -x -q`
   (on a sane host — idc02 load flakes git subprocesses).
2. `python3 opencode/ada_mcp.py` → stderr shows `ada-control bridge git-<sha> starting`.
3. Call `ada_bridge_info` → `bridge.version` + `tools_count`; check
   `~/.local/share/ada-control/calls.jsonl` for startup + per-call
   `bridge_version`.
4. Drop a loose `ada_mcp.py` copy inside any checkout → its version must
   carry `+copy-<hash>`, never bare `git-<sha>`.

lessons:
- idc02 load spikes (26+/4 cores) SIGKILL short-lived git subprocesses; pytest failures there can be starvation, not regression — retry before debugging code.
- A retry-while-running spawns duplicate dispatches on sibling branches; cherry-picking the sibling's pushed commit makes the outcome merge-safe either way.
- `git describe --dirty` ignores untracked files — any version scheme built on it needs an explicit untracked-file tag or loose copies collide with canonical.
- tests/dep_stubs.py provides a `mcp` stub so ada_mcp.py loads and its audit wrapper is exercisable on hosts without the mcp wheel (e.g. idc02) — real stdio probes still need a host with the dep installed.
- `~/.local/share/ada-control/calls.jsonl` on each host is now direct evidence of which bridge copy served which session — check it before assuming a deploy took.
