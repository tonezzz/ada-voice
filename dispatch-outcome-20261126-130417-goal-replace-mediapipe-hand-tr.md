# Dispatch outcome — rebuild-gesture-control-using-yolo-83cf4a

Card: rebuild-gesture-control-using-yolo-83cf4a (OPS-1436)
Runner: tony-macbook · repo: ada-voice · branch: dispatch/20261126-130417-goal-replace-mediapipe-hand-tr (commit dd8a928, not pushed)

## What the card asked vs what exists

"Replace MediaPipe hand tracking with a YOLO-based hand/gesture detection
model in the client display app (display UI / vcast client)."

The client display app is NOT in ada-voice — it lives in the chaba repo
(`stacks/web/public/apps/{vcast,gesture}/`, `experiments/ultralytics-yolo-ha/`).
Audit findings:

- **Already YOLO end-to-end (chaba, 2026-10-15, OPS-0440):** vcast
  `gesture {mode:"hand"}` uplinks camera frames (~300ms) and the
  `yolo-gesture.service` (hand_yolov8n.pt, single-class hand detector)
  emits `{type:"gesture",x,y,wx,wy,hands,action?}` msgs — box centre =
  pointer, dwell 700ms = click, two hands = grab→pane zoom. The wand
  `/apps/gesture/?engine=yolo` is a bare camera uplink; MediaPipe remains
  only the default engine for direct wand/jam use.
- **In-browser eval merged today:** card
  explore-yolo-for-gesture-control-in-apps-8e27eb landed `/apps/eye/?det=yolo`
  — YOLO26n HaGRID 12-class gestures fully in-browser via onnxruntime-web
  (WebGPU EP, WASM fallback). CPU-wasm p50 ~419ms vs MediaPipe ~103ms;
  WebGPU is the viable path, CPU-wasm is not real-time.
- **Live gap found:** the yolo-gesture detector is DOWN everywhere —
  tony-omen:8785 refused, idc03:8785 refused (disabled per the 10-20
  move), mn01 unreachable, `/apps/yolo/api` 502, gesture-relay
  idc02:8794 alive with 0 controllers. So `mode='hand'` was publishing
  frames nobody consumed while `vcast_gesture` still returned ok.

## What changed (ada-voice, commit dd8a928)

- `backend/tool_runner/screens.py` — `vcast_gesture(mode='hand')` now
  probes `GET /pointer` on `ADA_YOLO_POINTER_URL` (default
  `http://tony-omen.taila0626a.ts.net:8785`) and returns a `detector`
  block `{engine, online, enabled, sinks, present, grabbing, last_error}`.
  Warns when: detector unreachable, `POINTER_SINK` empty, or sinks can't
  reach the screen (`vcast:auto` / `vcast:<N>` pass; `ws://` feeds
  gesture-relay screens, not vcast rooms).
- `backend/realtime_provider.py` + `backend/tool_guide.yml` — corrected:
  'hand' is the YOLO hand-pointer, not "MediaPipe hand tracking".
- `tests/test_tool_runner.py` — `VcastGestureDetectorTests`, 7 tests.
- `docs/ssot/jobs/ada/2026-11-26-vcast-yolo-hand-pointer.yml` — full
  audit + model evaluation + follow-ups trail.

## Verify

- `pytest tests/test_tool_runner.py -k VcastGesture` → 7 passed
- `pytest tests/test_tool_runner.py -x -q` → 299 passed
- `pytest tests/test_tools_loader.py tests/test_scenarios.py -x -q` → 47 passed
- `pytest tests/test_gesture_controls.py tests/test_frontend_contract.py` → 33 passed
- `python3 scripts/tool-lint.py` → clean (71 tools, cap 115)
- Live: `curl http://tony-omen.taila0626a.ts.net:8785/pointer` → refused
  (detector offline — the new warning path covers exactly this)

## Follow-ups (flagged on card + job doc)

- Ops: restart `yolo-gesture.service` on tony-omen (unit template in
  chaba `experiments/ultralytics-yolo-ha/`) or repoint
  `ADA_YOLO_POINTER_URL`.
- Chaba-side: `/apps/gesture/` still defaults `engine=mediapipe`; flip or
  retire once YOLO proves on-device.
- Sibling backlog cards cover the rest: rebuild-v-cast-gesture-control-
  with-yolo-6cfa19 (ONNX Web vs server streaming) and
  vcast-gesture-control-with-yolo-detectio-bd5d28.

lessons:
- The vcast/gesture client apps and the yolo-gesture detector live in the
  chaba repo (`stacks/web/public/apps`, `experiments/ultralytics-yolo-ha`),
  not ada-voice — a card naming "the client display app" dispatched to
  ada-voice needs an audit-first scope, not a blind port.
- mode='hand' was already YOLO end-to-end since 2026-10-15; the real gap
  was the detector service being down fleet-wide — check the backend
  before assuming the client needs rebuilding.
- Same-named yolo units differ per host (omen yolo-xiaomi retired,
  mn01's is the /apps/yolo API) — probe by surface, never by name.
- GitHub tonezzz/chaba is a partial mirror (board-api commits only);
  dispatch merges to origin/master aren't visible via the GitHub API.
