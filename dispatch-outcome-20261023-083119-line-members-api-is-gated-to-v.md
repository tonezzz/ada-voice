# Dispatch outcome — line-group-member-registry

Task: LINE's members API is gated to verified accounts (404 on
`group/members/userIds` since 2026-10-11), so room members can't be
pre-enumerated — registration must happen on first message. When a
message arrives in an allow-listed group from an unmapped userId, the
relay writes a registry doc (mddb `line-user-registry`) with userId,
groupId, display name if available, first-seen timestamp. The operator
then binds userId → caller in `LINE_USER_CALLERS`.

## What changed

`backend/line_relay.py` — first-seen member registry.

- `handle_event` calls `_note_group_member(group_id, user_id,
  src_type)` for every event carrying a `source.userId` in an
  allow-listed room — message or not, addressed or not, mapped or
  unmapped. First-seen means first activity; an unmapped stranger's
  unaddressed chatter is still a sighting worth recording.
- `_note_group_member` dedups to one mddb check per userId per process
  (`_reg_seen`), re-arms a failed write after `LINE_REGISTRY_RETRY_S`
  (300s), and fires `_register_member` as a background task so mddb
  latency (or the `/add` inline embed) never stalls the turn/refusal
  path.
- `_register_member` upserts mddb `line-user-registry/<userId>`
  (reads `prefer_leader` so a lagging follower can't regress
  `first_seen`). meta: `user_id`, `group_ids` (merged — a member can
  appear in several rooms), `display_name` when LINE shares it,
  `first_seen`/`last_seen`, `status` `pending`|`bound`, `caller` when
  bound. Existing `contentMd` is preserved (operator notes survive); a
  new doc's body names the userId, room, and the `LINE_USER_CALLERS`
  bind step. Mapped members register too — a `pending` doc flips to
  `bound` on the member's next message after binding, so the registry
  is self-maintaining. Emits `member_registered` / 
  `member_registry_failed` ops events (WP ops timeline).
- `_fetch_profile_name` extracted from `_sender_label` — the per-member
  `/v2/bot/{group|room}/<id>/member/<uid>` GET is now shared; a real
  displayName from a first-seer also seeds the `_names` label cache.
- Config: `LINE_REGISTRY_COLL` (default `line-user-registry`,
  `""`/`off`/`0` disables), `LINE_REGISTRY_RETRY_S` (default 300).
  `MDDB_BASE_URL` default already resolves to idc03's store — no unit
  env change needed on deploy.

`tests/test_line_user_registry.py` — 12 tests: pending on unmapped
first message, registration on unaddressed chatter, displayName
recorded + cache seeded, gated-profile name-less doc, bound on mapped
member, pending→bound flip with merged `group_ids` + preserved
`first_seen`/body, per-process dedup, failure re-arm after cooldown,
disable flag, no registration for unallowed group / DM sender, and a
raising registry never breaking the refusal path.

`tests/test_line_relay_group.py` — `_registry_store` stubbed to `None`
in `GroupEventTests.setUp` so the suite never writes to a real mddb
(the deploy gate runs on idc03 where one is live).

`.env.example`, `docs/kb/msg-lane-multihome-routing.md` — env +
"Member discovery — first-seen registry" runbook (allow-list the
group → watch first senders land → bind each userId → restart the
unit). `docs/ssot/jobs/ada/2026-10-23-line-group-member-registry.yml`
records the card, decisions, verification, followups.

## Verification

- `tests/test_line_user_registry.py`: 12/12 pass.
- LINE suite (group, parked, quota, video-link, reply-context,
  speaker-file-input): all pass.
- Full `pytest tests/` in a worktree-local `.venv` (httpx/websockets/
  pytest/uvicorn/pillow/numpy/qrcode installed — `.venv` is
  gitignored): **1633 passed, 7 skipped, 0 failures**.
- `python3 scripts/tool-lint.py`: clean.

## Deploy note

Deploy-only change — the `ada-line-relay` unit env needs nothing new.
Once live on idc03, KK's first message in the shared room lands a
`pending` doc keyed by her userId; bind it to the already-minted
`user-kk` key (ada-ha-kk-keys.json) in `LINE_USER_CALLERS` and restart
the unit — her next message flips the doc to `bound`.
