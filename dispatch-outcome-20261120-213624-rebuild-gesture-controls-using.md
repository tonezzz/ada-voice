# Dispatch outcome — rebuild-gesture-controls-using-yolo-190356 (OPS-0683)

## Result: delivered, committed on `dispatch/20261120-213624-rebuild-gesture-controls-using` (ae425e0), not pushed.

Second attempt at this card — the 20261117 dispatch died ~3s in at service start
and left no branch. This run is a full rebuild of the desk companion's gesture
controls on the shared YOLO-pose pipeline, replacing the old ada_v2 MediaPipe
HandLandmarker approach.

## What changed

- `backend/gesture_controls.py` (new) — `GestureClassifier`, a pure state machine
  over wrist keypoint tracks (unit-testable, no asyncio), plus
  `GestureControlService` which consumes `pose_service.results()` and fans
  events out via a condition-var `events()` generator.
  - Gesture map: **wave** → wake/arms the controller · **swipe** → nav
    (left/right, mirror-aware) · **palm-hold** → confirm (open settings) ·
    **drop** → dismiss (close panels + disarm).
  - False-positive guards: `require_arm` wave-to-wake (12s armed TTL),
    `arm_grace_s` post-wave settle, per-gesture cooldown, palm-hold fires once
    until the hand lowers, wrist min-score gate, drop disarms.
  - Settings persisted via `posture_store.monitor_state`; `active_service()`
    module seam exposes the running instance to tools.
- `backend/main.py` — service lifecycle; `GET /api/gesture/status`,
  `PATCH /api/gesture/settings` (validated), `WS /ws/gesture`
  (`gesture_status` snapshot then `gesture` events).
- `backend/tools.d/gesture_controls.py` + `manifest.yml` + `tool_guide.yml` —
  `status|enable|disable` voice control; honest error on camera-less instances.
- `frontend/` — `#gesture-panel` (state, enabled + wave-to-activate switches,
  gesture map, last event), `#gesture-indicator` toast, always-on `/ws/gesture`
  client; nav swipes wrap-cycle kiosk panels (habits/home/pose/detect/hardware).
- `tests/test_gesture_controls.py` — 16 tests: classifier geometry, arming,
  grace, cooldown, hold-lock, drop-disarm, settings validation/persistence,
  event fan-out.
- `tests/scenarios-live/gesture_controls.yaml` — smoke tier; routes to
  `gesture_controls` (not `vcast_gesture`).
- `docs/ssot/ssot.tool-surface.yml` — count_cap 114→115.
- `docs/ssot/jobs/ada/2026-11-20-gesture-controls-yolo.yml` — job record.

## Why this shape

The kb-system assessment (recalled by `tests/scenarios-live/gesture_recall.yaml`)
concluded YOLO boxes are the wrong shape for a MediaPipe-style pointer UI but
viable for room-scale gestures. The desk companion already runs YOLOv8-pose
(Hailo `yolov8s_pose`, CPU MoveNet fallback) which emits wrist landmarks — so
discrete gestures were the right rebuild, not a cursor. Wave-to-arm mirrors the
vcast "wave-to-wake" design note and keeps desk noise from navigating the UI.

## Verification

- `python3 -m pytest tests/test_gesture_controls.py` — 16/16 pass
- `python3 scripts/tool-lint.py` — clean
- `python3 -m pytest tests/ -x -q` — 1870 passed, 7 skipped (full suite green)
- `frontend/app.js` parses (`node -e new Function`)
- On-device check after deploy: open the kiosk, wave at the camera → toast
  "Wave — gestures armed"; swipe → panels cycle; hold → settings; drop → close.

## Not done

- Pointer-class UX (pinch-click/fist-drag) — sibling card OPS-0440 owned the
  YOLO-bbox pointer experiment in the chaba repo; the kiosk has no cursor.
- vcast display `hand` mode — different codebase (input-bridge on tony-dell).
- Deploy — `deploy-ada.sh` on idc03 is the only path; not run here.

lessons:
  - Wave reversal counting must anchor on each leg's extremum and only count legs formed after the previous wave fire — otherwise the fired oscillation re-qualifies forever and eats real swipes.
  - A ~1s post-wave grace is required or the settle-back hand travel registers as a navigation swipe.
  - Window-coverage hold detection saturates below threshold at low frame rates (2fps) — a hold-anchor timestamp is frame-rate independent.
  - kanban card YAMLs live outside the ada-voice worktree; use `GET /card/<id>` on board-api for spec/comms.
  - MDDB kb-system collection was empty on tony-omen — scenario YAMLs embed the needed ground truth when docs aren't retrievable.
