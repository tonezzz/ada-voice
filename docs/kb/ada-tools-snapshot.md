# ada tools snapshot — producer/consumer transport

Card `ada-capabilities-verb-wiring` (contract: chaba
`docs/kb/ada-tools-verb-contract.md`, schema 1).

## Flow

```
ada-voice decls ──► chaba scripts/ada/tools-commands-catalog.py
                   ──► chaba docs/kb/ada-tools-commands.json
                   ──► deploy-ada.sh ──► backend/data/ada-tools-snapshot.json
                                          └─► tools.d/ada_capabilities reads it
```

- **Producer** lives in chaba and reruns on declaration-source change
  (`--check` is its drift gate). ada-voice never recomputes the catalog.
- **Transport** is a file copy at deploy time, one direction only:
  `scripts/deploy-ada.sh` step 2b copies
  `$CHABA_REPO/docs/kb/ada-tools-commands.json` (default
  `~/CascadeProjects/chaba`) to `backend/data/ada-tools-snapshot.json`,
  atomic `mkstemp`+`os.replace`, schema-1-validated. A missing/invalid
  source warns but never blocks the deploy — the tool degrades to
  `ok:false` per contract.
- **Consumer** is `backend/tools.d/ada_capabilities.py`
  (`policy=read`, `secondary_allowed=true`, `timeout_s=5`): one
  mtime-keyed read + deterministic template. `topic=` gives detail mode
  per tool/family/area.

## Ops notes

- `backend/data/` is gitignored (`data/`) — the snapshot is a runtime
  artifact, never committed to ada-voice.
- `ADA_TOOLS_SNAPSHOT` overrides the path (tests/staging).
- Failure modes per contract: missing/corrupt → `ok:false`
  "capability list not generated"; `source.behind > 0` or
  `generated_at` > 30 d → answer continues with an "as of" caveat;
  `schema != 1` → refuse with the version note.
- Absorbed `_ALIASES` names are filtered out of `tools[]` at read time —
  they route but are never listed as capabilities.
- To refresh without a full deploy on a host: re-run the chaba producer,
  then copy the JSON into place (the loader picks up the new mtime
  without a restart).
