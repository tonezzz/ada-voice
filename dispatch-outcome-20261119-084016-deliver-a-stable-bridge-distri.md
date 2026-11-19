# Dispatch outcome — stable bridge distribution for OpenChamber/OpenCode MCP clients

**Card:** ada-ocb-bridge-stable-path · **Branch:** `dispatch/20261119-084016-deliver-a-stable-bridge-distri` · **Commit:** 77b9622 (repo-side only, not pushed)

## What changed

- **`scripts/sync-ocb-bridge.sh`** (new, +x) — manages a dedicated ff-only
  worktree (default `~/CascadeProjects/ada-bridge`, owned by
  `~/CascadeProjects/ada-voice`). Creates the worktree detached at
  `origin/main` when absent; fetches and fast-forwards a stale checkout;
  **refuses** (exit 2, touches nothing) a dirty tree, local commits, a
  non-git occupier at the path, or a missing/untracked
  `opencode/ada_mcp.py`; `--dry-run` fetches and reports without mutating.
  Env overrides: `ADA_BRIDGE_CHECKOUT`, `ADA_VOICE_REPO`, `ADA_REMOTE`,
  `ADA_BRANCH`.
- **`scripts/ada-ocb-bridge-sync.service` / `.timer`** (new) — systemd USER
  units, 15-min `OnCalendar` + `Persistent`; a refused sync fails the unit
  so drift is visible via `journalctl --user -u ada-ocb-bridge-sync`.
- **`tests/test_sync_ocb_bridge.py`** (new, 9 tests) — builds a fake
  origin/checkout/worktree fixture in tmp_path; covers create,
  fast-forward, no-op, dry-run report-only, refuse-dirty (both
  `opencode/ada_mcp.py` and an unrelated file, incl. under `--dry-run`),
  refuse local commits, refuse non-git occupier.
- **`docs/kb/ada-ocb-bridge.md`** (new) — canonical path, mechanics, timer
  install, refusal repair, and the manual cutover snippet repointing
  `mcp.ada-control.command` in `~/.config/opencode/opencode.json` and
  DevNest `.opencode/opencode.json` to the stable path.
- **`AGENTS.md`** — new "ada-control bridge stable path (ocb)" section.
- **`docs/ssot/jobs/ada/2026-11-19-ocb-bridge-stable-path.yml`** — SSOT
  trail.

## Design notes

- Detached HEAD (not a local branch) — the checkout structurally can't
  accumulate branch state; local commits are detected via
  `rev-list origin/main..HEAD` and refused.
- Because the path is a real worktree, `ada_mcp.py`'s existing
  `_resolve_bridge_version()` (`git describe --always --dirty --tags`)
  names the exact origin/main commit serving each MCP session — no
  VERSION stamping needed.
- Timer chosen over a deploy-ada.sh hook: keeps deploy script untouched
  (it has a self-drift refusal gate) and covers hosts where ada-voice
  isn't deployed but clients run.

## Verify

- `python3 -m pytest tests/test_sync_ocb_bridge.py -q` → **9 passed**;
  `scripts/tool-lint.py` → clean.
- Live demo on a tmp repo: `--dry-run` on a stale checkout printed
  `would fast-forward … b5edb03 -> f976318` and left HEAD put; a dirty
  checkout exited 2 with `REFUSE: bridge checkout is dirty (opencode/
  ada_mcp.py itself is dirty)` — edits and HEAD untouched.

## Post-merge manual steps (host-side, not done per card spec)

1. Run `~/CascadeProjects/ada-voice/scripts/sync-ocb-bridge.sh` once
   (creates `~/CascadeProjects/ada-bridge`).
2. `cp scripts/ada-ocb-bridge-sync.{service,timer} ~/.config/systemd/user/`
   then `systemctl --user daemon-reload && systemctl --user enable --now
   ada-ocb-bridge-sync.timer`.
3. Repoint `mcp.ada-control.command` in `~/.config/opencode/opencode.json`
   and DevNest `.opencode/opencode.json` to
   `/home/tony/CascadeProjects/ada-bridge/opencode/ada_mcp.py`, restart the
   client, verify `ada_bridge_info` reports `git-<sha>` == origin/main.
