# Dispatch outcome — vcast-gesture-control-with-yolo-detectio-bd5d28

Card: vcast-gesture-control-with-yolo-detectio-bd5d28 (MED-1064)
Runner: idc02 · repo: ada-voice (= ada-pi post-rename; Tony's 2026-11-27
batch decision pinned repo=ada-voice, target = idc02 display node)
Branch: dispatch/20261127-083443-vcast-gesture-control-with-yol

## What the card asked vs what exists

"Use YOLO instead of MediaPipe for hand-gesture tracking on vcast
displays to keep device load low."

The vcast/gesture client apps and the yolo-gesture.service detector live
in the **chaba** repo (`stacks/web/public/apps/{vcast,gesture}/`,
`experiments/ultralytics-yolo-ha/`), outside this worktree. Per the
canonical card's audit (job doc
`docs/ssot/jobs/ada/2026-11-26-vcast-yolo-hand-pointer.yml`):

- vcast `gesture {mode:"hand"}` has been YOLO end-to-end since
  2026-10-15 — the display uplinks camera frames and
  `yolo-gesture.service` (hand_yolov8n.pt, box-centre pointer) emits the
  `{type:gesture}` msgs. MediaPipe remains only the default engine for
  direct wand/jam use in `/apps/gesture/` (chaba-side follow-up).
- The real ada-voice gap: `vcast_gesture(mode='hand')` answered bare
  `ok` while the detector was down fleet-wide, and the tool surface
  still described 'hand' as "MediaPipe hand tracking".

## What changed

The canonical card `rebuild-gesture-control-using-yolo-83cf4a` already
produced the fix — commits `dd8a928` + `3ca1ffc` on
`dispatch/20261126-130417-goal-replace-mediapipe-hand-tr` — but the
merge-sweep falsely marked it "already in origin/main" and it never
landed (merge `5da3cc0` does not exist upstream; branch still on
origin). This dispatch cherry-picked both commits cleanly onto
`0a5dbd8` and added:

- `tests/scenarios-live/vcast_gesture_ops.yaml` — stale comment
  "hand=MediaPipe desk UX" corrected to the YOLO hand-pointer.
- `docs/ssot/jobs/ada/2026-11-27-vcast-yolo-hand-pointer-landing.yml` —
  SSOT trail for this landing.

Landed code (from the cherry-picks):

- `backend/tool_runner/screens.py` — `_yolo_pointer_status()` probes
  `GET /pointer` on `ADA_YOLO_POINTER_URL` (default
  `http://tony-omen.taila0626a.ts.net:8785`);
  `vcast_gesture(mode='hand')` returns a `detector` block and warns when
  the detector is unreachable, `POINTER_SINK` is empty, or its sinks
  can't reach the target screen (`vcast:auto` / `vcast:<N>` only).
- `backend/realtime_provider.py` + `backend/tool_guide.yml` — 'hand'
  described as the YOLO hand-pointer, not MediaPipe.
- `tests/test_tool_runner.py` — `VcastGestureDetectorTests` (7 tests).
- `docs/ssot/jobs/ada/2026-11-26-vcast-yolo-hand-pointer.yml` +
  `dispatch-outcome-20261126-130417-*.md` — canonical audit trail.

## Result / verify

- `pytest tests/test_tool_runner.py -k VcastGesture` → 7 passed, 2 subtests
- `pytest tests/test_tool_runner.py -x -q` → 303 passed
- `pytest tests/test_gesture_controls.py tests/test_frontend_contract.py
  tests/test_scenarios.py -x -q` → 66 passed
- `pytest tests/ -x -q` (full suite) → 2053 passed, 8 skipped, 33 subtests
- `python3 scripts/tool-lint.py` → clean (74 declared, cap 116)
- `python3 scripts/scenario-schema-check.py` → 214 files, 0 violations
- Tests run with the main checkout's venv:
  `/home/tony/CascadeProjects/ada-voice/.venv/bin/python -m pytest`
  (system python3 lacks httpx/pytest)

## Follow-ups (not doable inside this worktree)

- **Ops**: `yolo-gesture.service` is down on every probed host
  (tony-omen:8785 refused) — hand mode now warns honestly until it is
  restarted (chaba `experiments/ultralytics-yolo-ha/yolo-gesture.service`)
  or `ADA_YOLO_POINTER_URL` is repointed.
- **Chaba repo**: `/apps/gesture/` still defaults `engine=mediapipe`
  for direct wand use; flip the default or retire the MediaPipe bundle
  once YOLO proves on-device — needs a chaba-side card/dispatch.

lessons:
- merge-sweep "already in origin/main — verified" can be a false
  positive: always `git log origin/main` / grep for the feature before
  trusting a held-merge card's verified mark (5da3cc0 never existed).
- The vcast/gesture client + yolo-gesture detector live in chaba
  (`stacks/web/public/apps`, `experiments/ultralytics-yolo-ha`), not
  ada-voice — repo-pinned dispatches to ada-voice can only land the
  tool-surface/honesty half; client work needs a chaba dispatch.
- System python3 on idc02 has no pytest/httpx; use the main checkout's
  venv `/home/tony/CascadeProjects/ada-voice/.venv/bin/python -m pytest`
  from the worktree (cwd picks up worktree tests, venv supplies deps).
- Same-named yolo units differ per host (omen yolo-xiaomi retired,
  mn01's is /apps/yolo API) — probe by surface, never by name.
