# Dispatch outcome: pwa-intake-persist-uploads (ADA-0589)

## What changed

PWA document intake (`POST /api/documents/intake` →
`DocumentCheckEngine.intake()`) held everything in RAM
(`document_check._held`, cap 20). The original upload never touched
disk, so a restart — or eviction before `ada_doc_archive` ran — lost
the file forever (Tony lost 2 UPS photos Oct 10, unrecoverable).

Fixed inside the single intake funnel (`backend/document_check.py`),
so every pathway (REST, ToolRunner held-key resolution, future
callers) is covered:

- New `persist_upload()` writes the raw bytes to
  `$ADA_DOC_INTAKE_DIR` (default `~/.local/share/ada/intake`) as
  `<ts>-<stem>-<sha1>.<ext>` via tmp-file + `os.replace`, then
  appends a `manifest.jsonl` line in the LINE_MEDIA shape (sha256,
  mime, path, `intake_key`) so a media-inbox lane can pick uploads
  up after a restart.
- `intake()` now persists FIRST — before `Image.open` — and computes
  the intake key up-front so the disk filename pairs with the key.
  `source_path` is recorded on `DocumentResult.to_dict()` and in held
  meta. A persist failure appends a loud `result.warnings` entry but
  never crashes intake (card constraint).
- Bounded retention: prunes oldest files past `$ADA_DOC_INTAKE_KEEP`
  (default 200).
- `pwa/app.js` doc-note text updated: rendered results remain
  RAM-only, the original is saved on disk.
- Failed-decode responses keep the old contract (`key=None`, no
  pdf_url) — the key is only attached on success.

Deliberately out of scope: `ada_doc_archive` still ships the enhanced
`archive_jpg`, not the raw original — `source_path` in held meta is
the hook for a later "archive originals" lane.

## Files

- `backend/document_check.py` — persist-first intake + manifest
- `pwa/app.js` — doc-note text
- `tests/test_document_check.py` — +4 tests
- `docs/ssot/jobs/ada/2026-11-23-pwa-intake-persist-uploads.yml` —
  job trail

## Result / verification

- `python3 -m pytest tests/test_document_check.py -x -q` — 19/19
  pass (incl. new: byte-identical persist, manifest line, persist
  failure non-fatal, bad-bytes still persisted, keep-limit prune).
- `python3 -m pytest tests/ -x -q` — 1885 passed, 7 skipped.
- `python3 scripts/tool-lint.py` — clean.
- Manual instrumented run (`measure()` spy): raw file +
  `manifest.jsonl` exist on disk mid-pipeline — before RAM
  processing completes; bytes byte-identical to the upload. Meets
  the done-condition.

## How to verify live (post-deploy on idc03)

Upload a document in the PWA, then
`ls ~/.local/share/ada/intake/` — the raw file +
`manifest.jsonl` line appear even before the intake response
returns; the response JSON carries `source_path`.

## Note on attempt 1

The 2026-11-17 dispatch died at the devin process level in <2 min
(exit=1); no code ever landed from that branch. This run is a fresh
implementation.

lessons:
- Prior dispatch failure was process-level (exit=1 in <2min), not
  code — don't over-index on "failed approach"; check card comms for
  whether code ever landed.
- Card briefs may specify dir/manifest conventions (LINE_MEDIA
  manifest.jsonl shape) beyond the summary spec — fetch the full
  card early via the board API, not just the primer spec.
- `rm -rf` in a compound command gets the tool call rejected even for
  /tmp scratch dirs — use fresh unique dirs instead.
