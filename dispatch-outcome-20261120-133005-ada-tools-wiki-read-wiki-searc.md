# dispatch outcome — ada-tools-wiki-read-wiki-search-recall-t-8f32dd

## What the card asked
Ada cannot read the wiki she co-owns (Education/Reports migrated
2026-11-08). Spec: tools.d `wiki_read(title)` via board-api `/wiki/page`
mode=read, and `wiki_search(query)` via a `/wiki/search` door if needed.

## What I found (live probes + chaba board-api source)
- `/wiki/page` is **write-only** — every mode variant returns
  "wikitext required"; `/wiki/search`, `/wiki/read`, `/wiki/get` 404.
  The spec's premise (a read door exists) was wrong.
- Education:/Reports: are **Lockdown-protected** — anonymous api.php
  reads get `accessdenied`, and ada-voice holds no wiki creds, so all
  recall must ride board-api's bot login.
- Doors that DO exist and are reused: `/lesson` op=list|get
  `source:wiki` (Education: read), `/wiki/news-review` (PrivateNews:
  wikitext), `/report` mode=search (MDDB dossiers), and the MDDB
  `chaba-lessons` mirror (`meta.wiki_title` + `url` per doc —
  real semantic recall over the migrated corpus).

## What I built (ada-voice worktree, branch
## `dispatch/20261120-133005-ada-tools-wiki-read-wiki-searc`, commit 068522f)
- `backend/tools.d/wiki_read.py` — tries the spec'd `/wiki/page
  mode=read|parse` first (wins automatically when chaba lands it), then
  falls back: Education:* → `/lesson` op=get source=wiki, PrivateNews:*
  → `/wiki/news-review`. Uncovered namespaces (Reports:, News/, main
  space) fail honestly naming the gap instead of pretending.
- `backend/tools.d/wiki_search.py` — tries `/wiki/search` first, then
  composes: `chaba-lessons` vector search + `/lesson` op=list title
  match + `/report` candidates; deduped hits
  {title,url,source,score,snippet} feed straight into wiki_read.
- `manifest.yml` (+2, policy read, secondary_allowed:false — member
  knowledge), `ssot.tool-surface.yml` count_cap 112→114,
  `tool_guide.yml` usage entries, 2 scenarios-live yamls, 13 unit tests.
- Trail: `docs/ssot/jobs/ada/2026-11-20-wiki-read-search-tools.yml`.
- Follow-up card filed: **board-api-wiki-read-search-doors (KNB-0017)**
  — chaba-side mode=read|parse + /wiki/search doors (different repo,
  out of this worktree's scope; the tools already call them first).

## Verified
- `pytest tests/ -x -q`: **1854 passed, 7 skipped** (deploy gate).
- `scripts/tool-lint.py`: **clean** (only pre-existing stale-debt warns).
- Live e2e vs real board-api: `wiki_read Education:Ada-Deploy-Flow` →
  real wikitext + meta + canonical URL via the lesson door;
  `wiki_search "deploy flow"` → real Education: hits (score 1.0 exact
  title), via=fallback.

## How to verify post-deploy
- Voice: "read the wiki page Education:Ada-Deploy-Flow" → calls
  wiki_read, narrates the lesson; "search the wiki for X" → wiki_search.
- `python3 scripts/tool-lint.py` stays clean; scenarios
  `wiki_read`/`wiki_search` under tests/scenarios-live (tier full).

## Caveats
- Coverage today: Education:*, PrivateNews:*, report dossiers. Reports:,
  News:/published news, templates and main space return an honest error
  until KNB-0017 lands the generic doors — then these tools upgrade
  with no ada-voice change.
- wiki_search's MDDB leg needs `runner.context.mddb` (present in
  production; absent → doors still searched).

lessons:
- Card spec claimed board-api /wiki/page mode=read exists — it is
  write-only; probe live endpoints before trusting spec premises (the
  card comm spec text is also truncated at ~500 chars — read the card
  YAML in the chaba repo for the full text).
- board-api write validation order is `from` → `title` → `wikitext`;
  error strings double as the endpoint contract when probing.
- Education:/Reports: are MediaWiki-Lockdown namespaces — anon api.php
  gets accessdenied; ada-voice holds no wiki creds by design, all wiki
  IO must ride board-api's bot login.
- The MDDB chaba-lessons mirror carries meta.wiki_title+url per doc —
  it is the working semantic search surface for Education: today.
