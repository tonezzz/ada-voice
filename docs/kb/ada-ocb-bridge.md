# ada-control bridge — stable distribution path

**Problem:** OpenCode/OpenChamber MCP clients ran `ada_mcp.py` out of the
edit/staging checkout (`~/CascadeProjects/ada-voice/opencode/`). That
checkout can be dirty, mid-rebase, or behind `origin/main`, so MCP children
silently served whatever revision happened to be on disk.

**Fix:** a dedicated ff-only worktree exists solely to distribute the bridge:

```
~/CascadeProjects/ada-bridge        # detached HEAD, only ever at origin/main
└── opencode/ada_mcp.py             # <- canonical MCP command target
```

The checkout is managed by `scripts/sync-ocb-bridge.sh` (in this repo,
canonical copy — like `deploy-ada.sh`, edit it in ada-voice, not on the
host):

- creates the worktree (`git worktree add --detach … origin/main`) if absent
- fetches `origin` and fast-forwards the checkout to `origin/main`
- **refuses** (exit 2, touches nothing) a dirty tree, local commits, a
  non-git occupier at the path, or a missing `opencode/ada_mcp.py`
- `--dry-run` reports what would change without touching the checkout

Overrides: `ADA_BRIDGE_CHECKOUT`, `ADA_VOICE_REPO`, `ADA_REMOTE`,
`ADA_BRANCH` env vars (tests use them; defaults match the fleet layout).

Because the path is a real worktree, the bridge version envelope
(`ada_bridge_info`, `calls.jsonl`) resolves `git describe --always --dirty
--tags` and names the exact `origin/main` commit that served each session —
no `VERSION` stamping needed.

## Timer (keeps the bridge from lagging main)

User units ship in the repo: `scripts/ada-ocb-bridge-sync.service` +
`scripts/ada-ocb-bridge-sync.timer` (every 15 min, Persistent).

```sh
cp scripts/ada-ocb-bridge-sync.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ada-ocb-bridge-sync.timer
```

A refused sync fails the unit on purpose — it means someone hand-edited or
committed in the bridge checkout. Inspect:

```sh
journalctl --user -u ada-ocb-bridge-sync -n 30
git -C ~/CascadeProjects/ada-bridge status   # must stay clean
```

Repair = remove the stray edits/commits (the checkout must only ever track
`origin/main`); never commit in the bridge checkout.

## Cutover (manual, per client — done once after merge)

Point `mcp.ada-control.command` at the stable path with an absolute path:

- `~/.config/opencode/opencode.json` (host OpenCode)
- DevNest `.opencode/opencode.json`

```json
"mcp": {
  "ada-control": {
    "type": "local",
    "command": ["python3", "/home/tony/CascadeProjects/ada-bridge/opencode/ada_mcp.py"],
    "enabled": true,
    "timeout": 120000
  }
}
```

(`python3` → the interpreter the bridge needs; keep whatever interpreter the
existing entry uses, only the script path changes.) Restart the client
afterward; verify with the `ada_bridge_info` tool — `bridge.version` reads
`git-<sha>` matching `origin/main`.

**Repo-side note:** nothing in this repo edits live host configs — the
repoint is a deliberate manual step so cutover timing is operator-chosen.
