# dispatch-outcome: ada-ocb-bridge-version (attempt 6, idc02 — zombie-claim restart)

Card: `ada-ocb-bridge-version` — BRIDGE_VERSION in `opencode/ada_mcp.py`,
`bridge{version,tools_count}` envelope on every tool response,
`ada_bridge_info` tool, `bridge_version` in calls.jsonl.

## What happened this run

This dispatch is a **restarted zombie claim** (OPS-1071): attempt 5's
claim committed but its response was lost, so the runner re-spawned the
card at 11:30. No code work was needed — the feature and its follow-up
fix were already merged to `main` before this worktree was cut:

- `83c8098` (11-18): BRIDGE_VERSION resolution (env > VERSION file >
  git describe > content hash), top-level `bridge` envelope via
  `_with_bridge_envelope`, `bridge_version` + `startup` event in
  calls.jsonl, stderr startup line, `ada_bridge_info()` tool.
- `b1059fc`/`361723f` → merged `d1881f2` + `73949e4` (11-23 11:01):
  `+copy-<sha256(file)[:10]>` suffix when `ada_mcp.py` is untracked in
  the repo `git describe` resolved — closes the loose-copy collision.

Every prior "failed" journal entry for this card was a runner
exit-code event *after* the work completed, not a code failure.

## Verification (this worktree, merged `main` @ `05ba2b3`)

- `pytest tests/test_opencode_ada_mcp_bridge_version.py
  tests/test_opencode_ada_mcp_envelope.py` — **19/19 pass** (2.4s).
- `python3 scripts/tool-lint.py` — clean (60 warnings, all debt-class).
- Live child-process probes (`python3 ada_mcp.py` real `__main__`,
  `ADA_CONTROL_AUDIT_DIR` pointed inside the worktree), four copies:

  | copy | startup version |
  |---|---|
  | canonical (tracked, this worktree) | `git-05ba2b3` |
  | untracked copy inside worktree | `git-05ba2b3+copy-3cd10becaa` |
  | untracked copy in foreign git repo | `git-a735b90+copy-106e3e3eb0` |
  | `ADA_BRIDGE_VERSION=2.1.0-deploy-test` | `2.1.0-deploy-test` |

- **Session evidence**: calls.jsonl recorded one `startup` event per
  spawned child (version + pid + cwd) and `bridge_version` on every
  per-call entry — `ada_bridge_info` calls from the canonical and stale
  copies logged `git-05ba2b3` vs `git-05ba2b3+copy-3cd10becaa`
  respectively, each with `ok: true`, `tools_count: 30` in the envelope.
- Probe scratch (`.bridge-verify/`) deleted via `find -delete` after the
  run; tree left clean.

## State / deliverables

Feature is complete on `main`; nothing to merge. Canonical docs from
prior attempts: `reports/2026-11-23-ada-ocb-bridge-version-copy-tag.md`,
`docs/ssot/jobs/ada/2026-11-23-bridge-version-copy-tag.yml`,
`docs/ssot/jobs/ada/2026-11-17-bridge-version-envelope.yml`.

How to verify on a deployed host:
1. `python3 opencode/ada_mcp.py` → stderr `ada-control bridge <ver> starting`.
2. Call `ada_bridge_info` → `bridge.version` + `tools_count` + features.
3. `~/.local/share/ada-control/calls.jsonl` → `startup` + per-call
   `bridge_version` identifies which copy served each MCP child.
4. A loose copy inside any checkout must report `+copy-<hash>`, never a
   bare `git-<sha>`.

lessons:
- A "restarted zombie claim" (OPS-1071) can spawn a dispatch whose work is already merged — check card comms + git log for merge commits before re-implementing; verify, don't rebuild.
- Real child-process probes work on hosts without the mcp wheel: drop a `sitecustomize.py` that calls `tests.dep_stubs.install()` into a PYTHONPATH dir, then `python3 ada_mcp.py` runs the true `__main__` path (startup line + audit event); `FastMCP.run()` no-ops so the process exits cleanly.
- `ADA_CONTROL_AUDIT_DIR` redirects calls.jsonl — point it inside the worktree for probes instead of touching `~/.local/share/ada-control/`.
