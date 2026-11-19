# ada-voice — agent operations

## Deploy flow (idc03/idc02)

`origin` = `github.com/tonezzz/ada-voice`, branch `main` — single source of truth.

- **tony-dell** `~/CascadeProjects/ada-voice` — the edit point. Commit + push here.
- **idc02** `~/CascadeProjects/ada-voice` — staging checkout; keep clean, `git pull --ff-only`. Verify scenarios here before prod.
- **idc03** `~/CascadeProjects/ada-voice` — production checkout serving `ada-ha-tony`, `ada-ha-michael`, `ada-dev`, `ada-line-relay`, `ada-tg-relay` (migrated from idc01 2026-10-05; the separate `ada-pi-pwa.service` on :8001 was retired 2026-11-17; the PWA surface remains served by `ada-ha-tony` on :8002). **Deploy-only**: do not hand-edit. Deploy via `~/.local/bin/deploy-ada.sh` — canonical copy is `scripts/deploy-ada.sh` in this repo; install with `install -m 0755 scripts/deploy-ada.sh ~/.local/bin/deploy-ada.sh`. It snapshots any dirty tree to `wip/deploy-snapshot-*`, ff-merges `origin/main`, runs the pre-flight gates, restarts the services, verifies active.
- **Deploy gates** (post-merge, pre-restart — a failure aborts before any restart): mass-delete warn (a merged diff that shrinks a `backend/tools.d/` or `backend/tool_runner/` file past half without touching a covering scenario pauses until `DEPLOY_FORCE=1`), `python3 scripts/tool-lint.py`, `.venv/bin/python -m pytest tests/ -x -q`. Lint/test failures are hard stops — fix the tree; `DEPLOY_FORCE` only clears the mass-delete pause. After the gates the script drains live `/ws` sessions (`ADA_DRAIN_S`, default 45s, per-service `active_sessions` from `/api/health`) so a deploy doesn't cut a conversation mid-turn; expiry restarts anyway since transcripts append per turn.

Never `git switch -f` / `checkout -f` / `reset --hard` on a checkout another session may be using — `-f` discards uncommitted work silently (incident 2026-10-01). Snapshot first; the deploy script does this for you.

## ada-control bridge stable path (ocb)

OpenCode/OpenChamber MCP clients should point `mcp.ada-control.command` at the dedicated ff-only worktree `~/CascadeProjects/ada-bridge` (`opencode/ada_mcp.py`) — never the edit/staging checkout, which can be dirty or mid-rebase. `scripts/sync-ocb-bridge.sh` (driven by the `ada-ocb-bridge-sync` user timer) fast-forwards it to `origin/main` and refuses a dirty or diverged tree. Details + client cutover: `docs/kb/ada-ocb-bridge.md`.
