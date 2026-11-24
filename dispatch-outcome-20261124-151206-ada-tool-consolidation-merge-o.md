# Dispatch outcome — ada-tool-consolidation-merge-overlapping-615ba9

Card: ADA-1203 "Ada tool consolidation — merge overlapping CMS write surfaces".
Worktree: dispatch-wt-20261124-151206-ada-tool-consolidation-merge-o (tony-dell).
Attempt 2. The 2026-11-24 14:31 run (tony-macbook) already landed and
auto-merged the consolidation (merge 9ac9474); Tony re-queued the card
at 15:04 with review_kind=verify. This run is the verification pass —
plus one unrelated defect found and fixed during the audit.

## Verdict: consolidation is complete and correct

Re-audited every backend door named in the spec:

- **WP CPTs (boards.surf-thailand.com REST via wp_client)** — the real
  overlap. `wp_post`/`cpt_post`/`place_report`/`wp_chat_page` merged
  into canonical `wp` tool with action=post|record|place|chat_page;
  impl seats retained in tools.d/, old names alive as
  tool_runner._ALIASES + _ALIAS_ARG_DEFAULTS rows (verified live:
  `_resolve_alias('wp_post') -> ('wp', {'action':'post'})`).
- **board-api /report** — read-only today (wiki_search mode=search);
  place_report was its last writer and moved to WP on 2026-10-14
  (81e2760). No writer overlap remains.
- **board-api /publish** — single writer (devnest_news news->MediaWiki
  pipeline). No overlap.
- **MDDB miniapp CMS** — cms_publish_page/cms_read/cms_edit is the
  deliberate canonical 3-tool design from tools-merge-cms
  (docs/ssot/jobs/ada/2026-10-05-tools-merge-cms.yml): publish is "the
  big writer" kept separate by that card's own spec. Different sink
  from WP AND a different operation class (full-page replace with meta
  contract + confirm handshake vs surgical note/delete/automate/edit).
  Merging them would contradict the SSOT family record — left as-is.
- ada_render_video's internal wp_client render_job post is a side
  effect of the render flow, not a model-facing write surface.
- trip_calendar shells the chaba generator (CMS page family) — single
  tool for its function, gated like cms_publish_page.

Spec checklist re-verified: manifest.yml has the single `wp` entry
(seats unmanifested); tool_guide.yml carries the merged `wp` guide
with explicit CMS-vs-boards routing; scenario fixtures updated
(wp_post.yaml, wp_chat_page.yaml -> calls_any: [wp]) plus the required
tool_merge_wp.yaml family regression; ssot.tool-surface.yml records
the wp family; scenario-live.py LEGACY_TOOLS/LEGACY_ARG_DEFAULTS
mirror the alias map; benchmark.yml never listed the old names
(parity — wp scenarios aren't in any suite).

## Change made this run

- `backend/tools.d/ada_adobe_stock.py` — renamed `TOOL` -> `DECLARATION`.
  The module shipped 2026-11-08 (2eaa8d2) with its schema dict under
  the wrong name, so tools_loader rejected it ("missing DECLARATION
  dict") on every registry init — the tool has been silently absent
  from the live surface for ~2.5 weeks even though manifest.yml,
  tool_guide.yml, and coverage_debt all assume it live. The DECLARATION
  content already existed verbatim; this is a one-token fix, no
  interface invented. Registry now loads it (32/33 tools.d in this
  env; ada_consult is instances:[ada-dev]-gated by design).

## Verification

- `python3 scripts/tool-lint.py` — clean: 71 declared (38 builtin +
  33 tools.d), cap 115, 96 aliases, 64 scenario-covered, 30 debt
  (69 pre-existing warnings, unchanged).
- `uv run --with-requirements backend/requirements.txt --with pytest
  --with pytest-asyncio python -m pytest tests/ -x -q` — **1974 passed,
  1 skipped** (full deploy-gate suite, ~4min).
- Alias smoke (live import): all four old names resolve to `wp` with
  the correct implied action= merged under caller args.
- Registry smoke: `wp` declared (module wp, timeout 60,
  secondary_allowed), four seat modules correctly NOT declared,
  `ada_adobe_stock` now declared.
- `$TASK_DIR/answers.jsonl` absent — no late answers/comments.
- Card has no `pipeline: ci` field — card-pipeline not applicable.

## How to verify

```
python3 scripts/tool-lint.py
git diff   # only: ada_adobe_stock.py TOOL->DECLARATION,
           #       ssot job yml verify note, this file
uv run --with-requirements backend/requirements.txt python -c \
  "import sys; sys.path.insert(0,'.'); \
   from backend.tool_runner.common import _resolve_alias; \
   print(_resolve_alias('wp_post'))"
```

lessons:
- tools.d loader contract is DECLARATION-or-nothing — a schema dict
  named TOOL (or any other var) fails silently at registry init while
  tool-lint still counts the manifest key; registry smoke
  (`tools_loader.registry()`) is the only check that catches it.
- Re-check card comms before assuming a re-queued card needs new
  work: this card's spec was already satisfied by an earlier merged
  run; review_kind=verify meant verify, not re-implement.
- "One canonical write tool per sink" does not mean one tool per
  repo: cms_publish_page vs cms_edit is a deliberate split recorded
  in ssot.tool-surface.yml — audit the SSOT family record before
  merging builtin tool pairs.
