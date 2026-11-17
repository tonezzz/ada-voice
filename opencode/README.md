# ada-code deploy (aka opencode-ada)

**Naming (2026-11-08):** `ada-code` is the canonical name for Ada's OpenCode
responder — the surface where Ada answers via OpenCode + ada-control MCP.
`opencode-ada` / `ada-opencode` are the SAME thing (legacy names still used
by the hostname, quadlets, and volume dirs — treat as aliases, do not create
a second instance).

Canonical sources for the ada-code OpenCode instance (container on idc02,
serve :4096, UI at ada-ha.surf-thailand.com/chaba-admin/opencode-ada).

Deploy path: ~/.local/share/ada-code/workspace/
  opencode.json            -> workspace/opencode.json
  ada_mcp.py               -> workspace/.opencode/ada_mcp.py
  commands/ada.md          -> workspace/.opencode/commands/ada.md

Edit here, copy to the workspace, then `systemctl --user restart ada-code`.

**Bridge version (2026-11-17):** every tool response carries a top-level
`bridge` object `{version, tools_count}` and each `calls.jsonl` line records
`bridge_version`, so a session's audit trail proves which ada_mcp.py copy
served it. Resolution order: `ADA_BRIDGE_VERSION` env > `.opencode/VERSION`
file > `git describe --always --dirty` in the file's dir > content hash of
the file (`dev-<sha10>`). When deploying outside a git checkout, stamp it:
`git -C <repo> rev-parse HEAD > workspace/.opencode/VERSION`. The
`ada_bridge_info` tool returns version + tool list + features on demand.
