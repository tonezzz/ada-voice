# Dispatch outcome — fix-phantom-state-in-media-control-f67d35

**Task**: Fix the phantom state issue in the media_player verification
protocol (card ref MED-0493). Owner answer on the card: "ada-voice —
media tool must verify media_player state after the call, on idc03".

## Root cause

`control_entity`'s verify-after-actuation protocol
(`backend/tool_runner/ha.py` `_CONTROL_VERIFY` / `_control_verify`)
covered the adjustable media_player verbs — `volume_up/down`,
`volume_mute`/`mute` (attr delta) and `select_source` (attr target) —
but the five **state verbs** passed through `_control_verified`
untouched: `turn_on`, `turn_off`, `media_play`, `media_pause`,
`media_stop`. HA accepts those service calls on a dead or queue-less
player (HTTP 200, state unchanged) and the tool returned a bare
`ok:true` echo — the same phantom-ok class the 2026-10-09 volume fix
and the 2026-10-14 ghost/on-off fixes already covered for other verbs.

## What changed

- `backend/tool_runner/ha.py`
  - New verify kind `state_set`: the polled entity state must land in
    `spec["targets"]` (a set — media_player awake/off spellings differ
    per integration). First target-state poll returns `ok:true` early;
    a concrete non-target state at window close fails `ok:false` with
    `stayed '<state>' — expected one of '<set>'` + do-NOT-claim error.
    `unknown` stays inconclusive (`verify.ok=None`, never flips the
    call), matching the missing-attr/dead-read contract.
  - `_CONTROL_VERIFY` gained the five media_player state verbs with
    per-spec `polls`/`poll_s` overrides (spec keys now honored by
    `_control_verify`): `turn_on` 12×1.0s (device wake lags),
    `turn_off`/`media_play` 8×0.8s, `media_pause`/`media_stop` 6×0.7s.
    `buffering` counts as landed for play/wake; `turn_off` is strict
    `{off, standby}` — `idle` means still-on, so a cast that can't
    power off reports the honest failure.
  - No fail-fast/dead-break inside state_set polls — the before-state
    legitimately sits in the not-yet set and HA state lags.
- `tests/test_tool_runner.py` — 12 new `ControlEntityVerifyTests`
  cases (land/fail per verb, buffering-as-landed, lagging turn_on
  wake, strict turn_off→idle phantom, unknown-state inconclusive);
  `test_unverified_verbs_pass_through` retargeted to button press
  (every media_player/cover verb verifies now); `HaMergeAliasTests`
  turn_on alias assertion updated for the verify block.
- `tests/scenarios/control_entity_media_state.yaml` — new offline
  scenario, 8 steps covering land+swallow per verb, lagging wake,
  strict idle-off, and unknown-state inconclusive.
- `backend/tool_guide.yml` — `control_entity` entry now lists the
  media_player state verbs as verified and calls out 'idle' after
  turn_off = still on.
- `docs/ssot/jobs/ada/2026-11-24-control-entity-media-state-verify.yml`
  — SSOT trail.

## Result / how to verify

- `pytest tests/test_tool_runner.py` — 292 passed.
- `pytest tests/test_scenarios.py` — 32 passed (incl. new scenario).
- `pytest tests/test_home_assistant.py tests/test_command_lane.py` —
  51 passed.
- **Full suite** `pytest tests/ -x -q` — **1937 passed, 7 skipped**
  (~4min; warnings all pre-existing).
- `python3 scripts/tool-lint.py` — clean.

Run used the main checkout's venv
(`~/CascadeProjects/ada-voice/.venv/bin/python`) — the worktree has no
venv; imports resolve to this worktree (verified `ha.py` path).

Not committed/pushed per dispatch rules — changes are in this worktree
on `dispatch/20261124-075941-fix-the-phantom-state-issue-in`.

lessons:
- The dispatch worktree has no .venv — reuse the main checkout's
  `~/CascadeProjects/ada-voice/.venv/bin/python`; `import backend`
  resolves to the worktree when cwd is the worktree.
- control_entity verify kinds: 'state_set' (targets set, unknown =
  inconclusive) is the right shape for media_player state verbs —
  single-target state_target can't express per-integration awake/off
  spellings, and fail-fast/dead-break would false-fail lagging wakes.
- media_player turn_off target set must exclude 'idle' — idle means
  the device is still on; accepting it re-creates the phantom the card
  is about.
- Scenario ha_states_seq entries pop in call order per entity and the
  last entry repeats — give each scenario step its own entity to keep
  seq bookkeeping trivial.
