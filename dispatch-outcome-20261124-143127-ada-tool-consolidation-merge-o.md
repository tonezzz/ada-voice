# Dispatch outcome — ada-tool-consolidation-merge-overlapping-615ba9

Card: ADA-1203 "Ada tool consolidation — merge overlapping CMS write surfaces".
Worktree: dispatch-wt-20261124-143127-ada-tool-consolidation-merge-o (tony-macbook).
Prior attempts 2-4 all died on provider rate-limits with zero code changes;
this run landed the full diff. No push, no commit, no deploy.

## Audit finding (differs from card spec)

The spec named cms_publish_page / cms_edit / wp_post / cpt_post /
place_report as overlapping "CMS write surfaces". The real overlap was only
on the WordPress side: `wp_post`, `cpt_post`, `place_report`,
`wp_chat_page` are four tools.d writers that all hit boards.surf-thailand.com
REST via `backend/wp_client.py` — and `place_report` moved CMS->WP on
2026-10-14 (81e2760), which created the overlap. `cms_publish_page` /
`cms_edit` write to the MDDB miniapp CMS (ada-cms-pages ->
surf-thailand.com static lane) — a different sink — so they stay declared
and untouched.

## What changed

- `backend/tools.d/wp.py` (NEW) — canonical `wp` tool, tools.d drop-in.
  `run(runner, **args)` dispatches on `action=post|record|place|chat_page`
  via `_load_impl` to the retained sibling modules, which stay in the tree
  as implementation seats (per-action arg schemas and error handling
  untouched).
- `backend/tools.d/manifest.yml` — the four absorbed entries replaced by a
  single `wp` entry (module: wp, policy: read, secondary_allowed: true,
  timeout_s: 60).
- `backend/tool_runner/common.py` — `_ALIASES` rows
  wp_post|cpt_post|place_report|wp_chat_page -> wp, plus
  `_ALIAS_ARG_DEFAULTS` injecting the matching action= so old-name calls
  resolve unchanged.
- `docs/ssot/ssot.tool-surface.yml` — new `wp` family
  (merge_card: this card, canonical: wp, scenario: tool_merge_wp,
  absorbed: the four names); place_report + cpt_post removed from
  coverage_debt.
- `scripts/scenario-live.py` — LEGACY_TOOLS mirror + LEGACY_ARG_DEFAULTS
  updated to match the alias map.
- `backend/tool_guide.yml` — three separate entries merged into one `wp`
  guide entry covering all four actions (stale place_report text fixed).
- `backend/tools.d/{wp_post,cpt_post,place_report,wp_chat_page}.py` —
  header comments noting they are now absorbed implementation seats.
- `tests/scenarios-live/wp_post.yaml`, `wp_chat_page.yaml` — calls_any
  expectations updated old name -> wp.
- `tests/scenarios-live/tool_merge_wp.yaml` (NEW) — family regression
  scenario required by the tool-lint family contract.
- `tests/test_wp_tool.py` (NEW) — unit test for the dispatcher; stubs
  `_load_impl` via monkeypatch.
- `docs/ssot/jobs/ada/2026-11-24-wp-tool-consolidation.yml` (NEW) — SSOT
  job trail.

## Verification

- `python3 scripts/tool-lint.py` — clean: 70 declared (38 builtin + 32
  tools.d), cap 115, 96 aliases, 64 scenario-covered, 29 debt.
- `uv run --with-requirements backend/requirements.txt --with pytest
  --with pytest-asyncio python -m pytest tests/ -x -q` — 1958 passed,
  1 skipped (full deploy-gate suite, ~3min). System python3 lacks
  pytest/httpx/websockets; the worktree ships no .venv.
- Card has no `pipeline: ci` field — card-pipeline script not applicable.
- `$TASK_DIR/answers.jsonl` absent — no late answers/comments; all card
  requests already answered.

## How to verify

```
python3 scripts/tool-lint.py
uv run --with-requirements backend/requirements.txt --with pytest \
  --with pytest-asyncio python -m pytest tests/test_wp_tool.py -q
git diff --stat   # 11 modified + 4 new files
```

lessons:
- The card spec's "CMS write surfaces" overlapped two different sinks —
  audit the actual backend (wp_client.py vs MDDB cms) before merging;
  cms_publish_page/cms_edit correctly stayed out of scope.
- tools.d absorption pattern that works here: keep the four impl modules
  as seats, add a thin dispatcher whose run() _load_impl's the seat by
  action=, and let common.py _ALIASES + _ALIAS_ARG_DEFAULTS keep old names
  working — zero transcript/history breakage.
- tool-lint family contract requires a scenario per merged family
  (tool_merge_wp.yaml) plus updating calls_any in existing fixtures.
- Worktree has no .venv; run pytest via
  `uv run --with-requirements backend/requirements.txt --with pytest
  --with pytest-asyncio` (system python3 lacks pytest/httpx/websockets).
- dict.setdefault returns the dict — `d.setdefault(k, v) or result` never
  yields result; use an explicit local in test stubs.
