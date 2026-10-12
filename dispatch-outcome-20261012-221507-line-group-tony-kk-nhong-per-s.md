# Dispatch outcome — line-multihome-group (attempt 4)

Task: LINE group (tony+kk+nhong) — mention-triggered per-sender routing
to each home assistant via the one chaba bot (@339sbrov).

## State on entry

Attempts 2+3 (merged via cc7431e) already delivered the spec's
in-worktree items: mention parse, `LINE_NAME_BACKENDS`,
`LINE_ALLOWED_GROUP_IDS`, per-member `LINE_USER_CALLERS` identity with
no room admin fallback, `[persona->sender]` tags, voice corpus + DM
mirror, and `docs/kb/msg-lane-multihome-routing.md`. This session
reviewed the feature once more against the noise rule and found the
interview-room pin (24d6e3d, added after attempt 3) leaked across
members.

## What changed this session

`backend/line_relay.py` — the persona pin is now **per (room, sender)**:

- Room-scoped pin (`_room_pin[room]`) meant ANY member's mention pinned
  the whole room: every member's unaddressed chatter then routed to the
  last-named persona — the exact "all hit one backend" noise the spec
  prohibits, and a privacy leak (tony's unaddressed message landing on
  kk's kaewta). It also allowed pin theft: kk pins kaewta, tony mentions
  ada, kk's next unaddressed follow-up went to ada.
- Pins now key `(room, sender)`: a member's unaddressed follow-ups
  stick to the persona THEY named until they mention another (the
  switch). Members with no pin stay ignored — noise rule intact per
  member. Interview-room UX preserved (one active speaker behaves
  identically).
- Empty-URL personas (`chaba:`) are never pinned — an isn't-home reply
  must not stick the member to a dead backend.
- `_route` gained a `sender` param; both call sites (text/image route,
  voice-transcript route) pass `user_id`. Group images/transcripts
  follow the member's own pin (photo follows their conversation).
- Module docstring updated.

`tests/test_line_relay_group.py` — 23 → **28 tests**: pin routes
sender's follow-ups (session reuse), no cross-member leak, other
member's mention can't steal a pin, new mention switches the sender's
pin, offline persona not pinned. Note for future tests: sessions reuse
per `(chat, sender, persona)` — assert `send_turn` counts/args, not
session count.

Docs: `docs/kb/msg-lane-multihome-routing.md` (sticky-address rule
documented — it was previously undocumented and the noise-rule text was
stale vs the pin feature), `docs/ssot/apps/ssot.apps.ada-persons.yml`
(routing.sticky), `.env.example` group comment, and the job trail
`docs/ssot/jobs/ada/2026-10-11-line-group-mention-routing.yml` gained an
attempt-4 hardening section.

## Verification

- `PYTHONPATH=.teststubs python3 -m unittest tests.test_line_relay_group`
  — **28/28 pass**.
- `python3 -m py_compile backend/line_relay.py` — clean.
- `python3 scripts/tool-lint.py` — clean (61 warnings, pre-existing).
- Runner note: tony-macbook has **no pip/venv/pytest** — created
  `.teststubs/` (already gitignored for this) with minimal
  httpx/websockets stubs; tests mock all network so stubs suffice.

## Not changed (deliberate)

- Room-level vs per-member pin: Tony's 24d6e3d set room scope; this
  narrows it to per-member to keep the spec's noise rule. If the
  interview room truly wants whole-room stickiness, say so and it's a
  one-line revert (key `room` instead of `(room, sender)`).
- nhong `home_persona` in the persons registry stays `tbd` — personas
  in the room are ada|kaewta|chaba; chaba is the house guest tier
  (ada-chaba :8008 pending, per the rebrand commit 5c4afe0), not a
  per-member home backend.

## Remaining ops (outside worktree — idc03 host changes)

- `ada-line-relay` unit env: `LINE_ALLOWED_GROUP_IDS` (group id prints
  in the join greeting/log), `LINE_NAME_BACKENDS=ada:ws://127.0.0.1:8002,
  kaewta:ws://127.0.0.1:8007,chaba:`, `LINE_USER_CALLERS` entry for
  nhong, optional voice envs.
- `user-nhong` key minted on EACH reachable persona backend
  (`auth.create_key` / `ada_member_keys`), scoped to her tier per
  backend (kk = guest of ada-ha / member of tony-ha; tony = member of
  kk-ha — apply the same model).
- `chaba:` stays empty until ada-chaba :8008 is provisioned
  (chaba-backend card; CHABA_MEMORY=1 guest-tier instance per 5c4afe0).
