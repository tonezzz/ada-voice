# ada-ocb bridge version — untracked-copy collision fix (2026-11-23)

Card: `ada-ocb-bridge-version` (ADA-0177). Dispatch
`20261123-105125-in-ada-voice-opencode-ada-mcp-` on tony-omen.

## State found

The original feature landed 2026-11-18 (`83c8098`, merged via `aa31f29`):
`BRIDGE_VERSION`, top-level `bridge{version, tools_count}` envelope on
every tool response, `bridge_version` in `calls.jsonl` (per call +
`startup` event), stderr startup line, `ada_bridge_info()` tool.

**Gap fixed this run** (found by the 11-20 tony-omen run but never
merged): a loose `ada_mcp.py` copy sitting *inside* a git checkout
resolved that repo's `git describe`, and because `git describe --dirty`
ignores untracked files it reported the exact same `git-<sha>` as the
canonical bridge — the collision the version handshake exists to catch.

## Change (opencode/ada_mcp.py)

- `_untracked_copy_tag(here)` — after `git describe` succeeds, runs
  `git ls-files --error-unmatch -- <basename>` in the file's dir; if the
  file is not tracked by that repo, appends `+copy-<sha256(file)[:10]>`.
  A tracked file at the same sha IS canonical — no tag.
- `_file_digest()` helper shared by the `dev-` fallback and the tag.
- Resolution order now: `ADA_BRIDGE_VERSION` env > `opencode/VERSION`
  file > `git describe --always --dirty --tags` (+`copy-<hash>` when
  untracked there) > `dev-<content hash>`.
- `ada_bridge_info` features text updated to describe the tag.

## Tests (tests/test_opencode_ada_mcp_bridge_version.py — 9 tests)

- `test_untracked_copy_inside_checkout_gets_copy_tag` — new
- `test_tracked_copy_inside_checkout_has_no_copy_tag` — new
- 19/19 pass together with `test_opencode_ada_mcp_envelope.py`;
  `scripts/tool-lint.py` clean.

## Live MCP stdio verification (real `initialize` + `tools/call ada_bridge_info`)

| copy | version |
|---|---|
| `opencode/ada_mcp.py` (tracked, dirty) | `git-00090e9-dirty` |
| untracked copy inside a foreign checkout | `git-fa4f5a6+copy-3cd10becaa` |
| untracked copy inside this worktree | `git-00090e9-dirty+copy-1462d6fd74` |

The third row is the exact pre-fix collision: it shares the canonical
`git-00090e9-dirty` base and would have been indistinguishable before the
tag. The `+copy-` hash also differs between two stale copies of
different content (`3cd10becaa` vs `1462d6fd74`).

## Session evidence (what an OpenChamber session leaves)

- stderr: `ada-control bridge <version> starting (tools=30 pid=… cwd=…)`
- `calls.jsonl` startup event: `{event:"startup", bridge_version, tools_count, cwd, pid}`
- `calls.jsonl` per call: `{bridge_version, tool, args, ok, …}`

`ADA_CONTROL_AUDIT_DIR` overrides the audit dir (default
`~/.local/share/ada-control`) for per-session capture.
