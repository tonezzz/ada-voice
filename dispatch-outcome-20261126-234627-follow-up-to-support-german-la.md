# Dispatch outcome — ada-voice CMS lang arg beyond en/th

Card: `open-ada-cms-tool-lang-arg-beyond-en-th-37f8a0` (follow-up to
`support-german-language-variants-on-cms-69ef23`). Host idc02,
branch `dispatch/20261126-234627-follow-up-to-support-german-la`.
Worktree diff only — nothing committed, pushed, or deployed.

## Which layer was the gate

**All three real layers, not just wording** — CMS_INSTRUCTIONS was
descriptive only; the actual wall was:

1. **Tool-declaration enum** — `enum: ["en","th"]` on `lang` in
   `backend/realtime_provider.py` for `cms_read`, `cms_publish_page`,
   `cms_edit` (note/lang + automate `langs` items). This is a HARD
   gate, not just model-facing: `_validate_call_args` in
   `backend/tool_runner/common.py` enforces `enum` server-side and
   returns SchemaError before the runner sees the call.
2. **Runner whitelists** — `if lang not in ("en","th")` in
   `cms_publish_page` and the `("en","th")` check in
   `_cms_automation_knobs`, both in `backend/tool_runner/cms.py`.
3. **Prompt wording** — CMS_INSTRUCTIONS said "en/th variants
   coexist" (now generalized); descriptions on the declarations also
   claimed en/th only.

The MDDB store itself was never the wall (prior card's finding
confirmed): `/add`/`/get` key docs by `(collection, key, lang)` and
accept any tag.

## What changed

- `backend/tool_runner/cms.py` — new `_CMS_LANG_RE` + `_cms_lang()`
  normalizer (`^[a-z]{2,3}(-[a-z0-9]{2,8})*$`, lowercase/strip,
  empty→en, junk→ValueError). Applied everywhere `lang` is accepted:
  `cms_get_page`, `cms_publish_page` (replaces the whitelist),
  `cms_note_update`, `_cms_edit_sections`, `_cms_automation_knobs`
  (per-item). `cms_get_page` fallback chain generalized: requested →
  en → th → **last-resort meta.slug variant discovery** so a
  de-*only* page is reachable via default reads (`fallback: True`
  marks it). Sibling public-flag check scans en+th variants.
  `cms_note_update` falls back en→th (th-only page now annotatable
  from an en request — previously not_found; nothing that worked
  breaks).
- `backend/realtime_provider.py` — removed `enum:["en","th"]` from
  all four `lang`/`langs` declarations; descriptions now read "any
  tag like 'en'/'th'/'de'"; CMS_INSTRUCTIONS wording generalized.
- `backend/tool_runner/__init__.py` — `cms_publish_page` pending-
  confirm key normalizes lang case (`DE`≡`de`) so register/resubmit
  casing drift can't strand a confirm.
- `tests/test_tool_runner.py` — +4 tests: arbitrary-lang publish
  (incl. case-drift confirm path), malformed-tag rejection, de read
  fallback + de-only discovery, note on de variant; automation knob
  test asserts `langs:[en,th,de]` lands and bad tags still fail.
- `tests/scenarios-live/cms_translate.yaml` — German probe section
  rewritten: 'de' is legal now; asserts a real `cms_publish_page`
  call and `'lang': 'de*'` in the tool_result (phantom-claim watch
  kept); cleanup deletes `de`/`de-de` too.
- `pwa_server.py` — `/api/cms/pages/{slug}` docstring updated.
- `docs/ssot/jobs/ada/2026-11-26-cms-lang-arbitrary-tags.yml` —
  job record.

Deliberately kept: the `-th` slug-suffix derivation hack stays
th-only — generalizing to any `-xx` suffix false-positives on
English slugs ending in valid lang codes (`as-is`→is, `tie-in`→in,
`check-it`→it). Other languages pass `lang=` explicitly.

## Verification

- `python3 scripts/tool-lint.py` — clean (74 declared tools).
- `python3 scripts/scenario-schema-check.py` — 214 files, 0 violations.
- `.venv/bin/python -m pytest tests/ -x -q` — **2046 passed,
  8 skipped** (~3:17). Worktree-local `.venv` built per KNOWN_PITFALLS
  (numpy, pillow<13, qrcode, fastapi, uvicorn, google-genai).
- **Live lang=de round-trip** against the real `ada-cms-pages` MDDB
  store (`:11027`, healthy): `cms_publish_page` slug
  `devin-langprobe-20261126` lang=de → pending-confirm →
  `{'status':'published','lang':'de','stored_verified':True}` →
  `cms_read` lang=de returns German content → default read discovers
  the de-only variant (`fallback:True`) → `cms_edit note` OK →
  `lang='bad lang!'` rejected ValueError → `/delete` verified gone.
  Probe files (`.probe_lang_de.py`, `.probe-tmp/`) removed.

## Known limitations (not in scope)

- `pwa/cms/index.html` viewer still has a hardcoded EN/ไทย toggle —
  a de page is reachable via `?lang=de` / the API but has no viewer
  button (frontend follow-up if wanted).
- `cms_note_update` has no exotic-variant discovery (en→th only);
  writes stay explicit.
- Lang tags are lowercased before storage — a chaba writer storing
  `de-DE` verbatim wouldn't match a `de-de` read (store treats lang
  as an opaque key part).
- Model may still bake `-de` into the slug instead of passing
  `lang=de` (the `-th` hack only covers th) — page lands as an en doc
  named `x-de`; the updated live scenario asserts the lang tag in
  `tool_result` so drift is visible.

## How to verify post-deploy

Deploy is operator-side (`deploy-ada.sh` on idc03). Post-deploy:
`cms_publish_page` a disposable slug `lang=de` → `cms_read
action='get' lang=de` returns the German content; or run the
`cms_translate.yaml` live scenario which now exercises the full
voice path.

lessons:
- enum in a tool declaration is a HARD gate, not model-facing
  decoration — `_validate_call_args` enforces it server-side with
  SchemaError before the runner runs; always check common.py's
  validator when a tool "rejects" an arg.
- uvicorn[standard] does NOT pull starlette; dep_stubs' starlette
  stubs then break `Route(...)` positional args — install `fastapi`
  (which pulls real starlette) before pytest on a fresh venv.
- Generalizing a suffix-derivation hack needs a false-positive audit:
  '-th' → any '-xx' would misread slugs like 'as-is'/'tie-in' as lang
  variants; keep suffix tricks per-tag and pass lang= for the rest.
- Worktree-local probe scripts against the live MDDB store are the
  cheapest live-verification path — publish→read→note→reject→delete
  round-trip took one script; always delete the probe doc + files.
