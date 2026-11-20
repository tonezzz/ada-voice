"""YOLO-pose gesture controls for the desk companion (card OPS-0683).

Rebuilds the old ada_v2 MediaPipe hand-tracking controls on the shared
YOLO-pose pipeline (Hailo yolov8s_pose, CPU MoveNet fallback): wrist
keypoint trajectories from PoseService are classified into discrete
control gestures that the kiosk UI consumes over /ws/gesture and Ada
can toggle through the gesture_controls tool.

Gesture map (all from wrist tracks — YOLO-pose already emits landmark
keypoints, so no pointer regressions from box-shaped output):

  wave       raised hand moved side-to-side    -> wake     (arms control)
  swipe      raised hand sweeps left/right     -> nav      (cycle panels)
  palm_hold  raised hand held steady           -> confirm  (open settings)
  drop       raised hand drops quickly         -> dismiss  (close/disarm)

Wave-to-arm gating (require_arm) means incidental desk movement cannot
fire navigation gestures — the controller sleeps until a deliberate wave.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from contextlib import suppress
from typing import Any

logger = logging.getLogger("voice.gesture_controls")

WRIST_KEYS = ("left_wrist", "right_wrist")
SHOULDER_KEYS = ("left_shoulder", "right_shoulder")
SETTINGS_KEY = "gesture_controls_settings"

GESTURE_ACTIONS = {
    "wave": "wake",
    "swipe": "nav",
    "palm_hold": "confirm",
    "drop": "dismiss",
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "require_arm": True,       # wave-to-wake before nav/confirm/dismiss
    "mirror": True,            # report swipe direction in user space
    "min_score": 0.30,         # wrist keypoint confidence gate
    "raise_margin": 0.05,      # wrist this far above shoulders = raised
    "raise_fallback_y": 0.42,  # fallback raised line when no shoulders
    "wave_min_travel": 0.08,   # lateral travel per reversal (frame frac)
    "wave_reversals": 2,       # direction changes within wave_window_s
    "wave_window_s": 1.8,
    "swipe_frac": 0.20,        # horizontal travel within swipe_window_s
    "swipe_window_s": 0.9,
    "swipe_max_dy": 0.12,      # max vertical drift during a swipe
    "hold_s": 1.4,             # raised + steady this long = palm_hold
    "hold_jitter": 0.045,      # max drift while "steady"
    "drop_frac": 0.16,         # vertical fall within drop_window_s
    "drop_window_s": 0.5,
    "armed_ttl_s": 12.0,       # arm expires without a gesture
    "arm_grace_s": 1.0,        # ignore swipes this long after a wave
    "cooldown_s": 1.2,         # per-gesture refire lockout
}

_SETTING_BOUNDS = {
    "min_score": (0.05, 0.95),
    "raise_margin": (0.0, 0.5),
    "raise_fallback_y": (0.1, 0.9),
    "wave_min_travel": (0.02, 0.5),
    "wave_reversals": (1, 6),
    "wave_window_s": (0.4, 5.0),
    "swipe_frac": (0.05, 0.8),
    "swipe_window_s": (0.2, 3.0),
    "swipe_max_dy": (0.02, 0.5),
    "hold_s": (0.4, 5.0),
    "hold_jitter": (0.005, 0.3),
    "drop_frac": (0.05, 0.6),
    "drop_window_s": (0.2, 2.0),
    "armed_ttl_s": (3.0, 120.0),
    "arm_grace_s": (0.0, 5.0),
    "cooldown_s": (0.2, 10.0),
}


def _points(keypoints: list[dict[str, Any]] | None) -> dict[str, dict[str, float]]:
    return {p["name"]: p for p in keypoints or []
            if isinstance(p, dict) and "name" in p}


class GestureClassifier:
    """Pure state machine over wrist keypoint tracks — no asyncio, so it is
    directly unit-testable. feed() returns an event dict or None."""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = {**DEFAULT_SETTINGS, **(settings or {})}
        self.armed_until = 0.0
        self._track: deque[tuple[float, float, float, str]] = deque(maxlen=64)
        self._last_fired: dict[str, float] = {}
        self._last_raised_at = 0.0
        self._last_raised_y = 0.0
        self._hold_since = 0.0
        self._hold_anchor = (0.0, 0.0)
        self._hold_locked = False

    def feed(self, now: float,
             keypoints: list[dict[str, Any]] | None) -> dict[str, Any] | None:
        s = self.settings
        if not s["enabled"]:
            self._track.clear()
            self.armed_until = 0.0
            return None
        pts = _points(keypoints)
        wrist, hand = self._best_wrist(pts, float(s["min_score"]))
        if wrist is None:
            self._expire(now)
            return None
        raised = self._is_raised(wrist, pts, s)
        x, y = float(wrist["x"]), float(wrist["y"])
        score = float(wrist.get("score", 0))
        self._track.append((now, x, y, "r" if raised else "d"))
        if raised:
            self._last_raised_at, self._last_raised_y = now, y
            if not self._hold_since or (
                    abs(x - self._hold_anchor[0]) > float(s["hold_jitter"])
                    or abs(y - self._hold_anchor[1]) > float(s["hold_jitter"])):
                self._hold_since, self._hold_anchor = now, (x, y)
        else:
            self._hold_since = 0.0
            self._hold_locked = False

        armed = self.armed(now) or not s["require_arm"]
        # Order matters: wave is checked in every state so a raised wave
        # re-arms; the armed-only gestures gate on `armed`.
        event = self._detect_wave(now) or None
        if event is None and armed:
            event = (self._detect_swipe(now)
                     or self._detect_hold(now)
                     or self._detect_drop(now))
        if event is None:
            self._expire(now)
            return None
        if event["gesture"] == "palm_hold" and self._hold_locked:
            return None
        if self._cooling(now, event["gesture"]):
            return None
        self._last_fired[event["gesture"]] = now
        if event["gesture"] == "palm_hold":
            self._hold_locked = True
        if event["gesture"] == "wave":
            self.armed_until = now + float(s["armed_ttl_s"])
        elif event["gesture"] == "drop":
            self.armed_until = 0.0
        else:
            self.armed_until = max(self.armed_until,
                                   now + float(s["armed_ttl_s"]))
        return {"gesture": event["gesture"],
                "action": GESTURE_ACTIONS[event["gesture"]],
                **{k: v for k, v in event.items() if k != "gesture"},
                "hand": hand, "confidence": round(score, 3),
                "armed": self.armed(now)}

    def armed(self, now: float) -> bool:
        return now < self.armed_until

    def _expire(self, now: float) -> None:
        if self.armed_until and not self.armed(now):
            self.armed_until = 0.0

    @staticmethod
    def _best_wrist(pts: dict[str, dict[str, float]],
                    min_score: float) -> tuple[dict[str, float] | None, str]:
        candidates = [(pts[k], "left" if k.startswith("left") else "right")
                      for k in WRIST_KEYS
                      if k in pts and float(pts[k].get("score", 0)) >= min_score]
        if not candidates:
            return None, "none"
        return max(candidates, key=lambda c: float(c[0]["score"]))

    @staticmethod
    def _is_raised(wrist: dict[str, float],
                   pts: dict[str, dict[str, float]],
                   s: dict[str, Any]) -> bool:
        shoulders = [float(pts[k]["y"]) for k in SHOULDER_KEYS
                     if k in pts and float(pts[k].get("score", 0)) >= 0.2]
        line = (min(shoulders) - float(s["raise_margin"])) if shoulders \
            else float(s["raise_fallback_y"])
        return float(wrist["y"]) < line

    def _window(self, now: float, span: float) -> list[tuple[float, float, float, str]]:
        return [s for s in self._track if now - s[0] <= span]

    def _cooling(self, now: float, gesture: str) -> bool:
        last = self._last_fired.get(gesture)
        return last is not None and now - last < float(self.settings["cooldown_s"])

    def _detect_wave(self, now: float) -> dict[str, Any] | None:
        s = self.settings
        # Only legs formed since the last wave fire count — otherwise the
        # oscillation that just fired would re-qualify forever and eat any
        # real swipe that follows the wave.
        last_wave = self._last_fired.get("wave", -1e9)
        raised = [t for t in self._window(now, float(s["wave_window_s"]))
                  if t[3] == "r" and t[0] > last_wave]
        if len(raised) < 3:
            return None
        # Track each leg's extremum; a reversal = the wrist established a
        # new leg of >= wave_min_travel in the opposite direction.
        reversals, travel, prev_dir = 0, 0.0, 0
        extreme_x = raised[0][1]
        for _, x, _, _ in raised[1:]:
            dx = x - extreme_x
            direction = 1 if dx > s["wave_min_travel"] else \
                -1 if dx < -s["wave_min_travel"] else 0
            travel = max(travel, abs(dx))
            if not direction:
                continue
            if prev_dir and direction != prev_dir:
                reversals += 1
            prev_dir = direction
            extreme_x = x
        if reversals >= int(s["wave_reversals"]):
            return {"gesture": "wave",
                    "detail": f"{reversals} reversal(s), travel {travel:.2f}"}
        return None

    def _detect_swipe(self, now: float) -> dict[str, Any] | None:
        s = self.settings
        # A wave ends mid-arc — let it settle before lateral travel counts.
        if now - self._last_fired.get("wave", -1e9) < float(s["arm_grace_s"]):
            return None
        window = [t for t in self._window(now, float(s["swipe_window_s"]))
                  if t[3] == "r"]
        if len(window) < 2:
            return None
        start, end = window[0], window[-1]
        dx, dy = end[1] - start[1], end[2] - start[2]
        if abs(dx) < float(s["swipe_frac"]) or abs(dy) > float(s["swipe_max_dy"]):
            return None
        direction = "right" if dx > 0 else "left"  # image space
        if s["mirror"]:
            direction = "left" if dx > 0 else "right"
        return {"gesture": "swipe", "direction": direction,
                "detail": f"dx {dx:+.2f} over {end[0] - start[0]:.2f}s"}

    def _detect_hold(self, now: float) -> dict[str, Any] | None:
        s = self.settings
        if self._hold_since and now - self._hold_since >= float(s["hold_s"]):
            return {"gesture": "palm_hold",
                    "detail": f"steady {float(s['hold_s']):.1f}s"}
        return None

    def _detect_drop(self, now: float) -> dict[str, Any] | None:
        s = self.settings
        span = float(s["drop_window_s"])
        if not self._track or self._track[-1][3] != "d":
            return None
        if now - self._last_raised_at > span:
            return None
        fell = self._track[-1][2] - self._last_raised_y
        if fell >= float(s["drop_frac"]):
            return {"gesture": "drop",
                    "detail": f"fell {fell:+.2f} in {span:.1f}s"}
        return None


_ACTIVE: "GestureControlService | None" = None


def active_service() -> "GestureControlService | None":
    """The service instance main.py started, for tools.d access."""
    return _ACTIVE


class GestureControlService:
    """Consume the shared PoseService stream and fan out control gestures."""

    def __init__(self, pose_service: Any, store: Any = None) -> None:
        global _ACTIVE
        self.pose_service, self.store = pose_service, store
        saved = store.monitor_state(SETTINGS_KEY) if store else {}
        self.settings = {**DEFAULT_SETTINGS, **(saved or {})}
        self.classifier = GestureClassifier(self.settings)
        self.last_event: dict[str, Any] | None = None
        self.last_event_at: float | None = None
        self.counts = {name: 0 for name in GESTURE_ACTIONS}
        self._generation = 0
        self._condition = asyncio.Condition()
        self._task: asyncio.Task[None] | None = None
        _ACTIVE = self

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def _run(self) -> None:
        try:
            async for result, _frame in self.pose_service.results():
                event = self.classifier.feed(
                    time.monotonic(), result.get("keypoints"))
                if event is None:
                    continue
                event = {**event, "state": self.state()}
                self.last_event, self.last_event_at = event, time.monotonic()
                self.counts[event["gesture"]] = \
                    self.counts.get(event["gesture"], 0) + 1
                logger.info("gesture %s -> %s (%s)", event["gesture"],
                            event["action"], event.get("detail", ""))
                async with self._condition:
                    self._generation += 1
                    self._condition.notify_all()
        finally:
            async with self._condition:
                self._condition.notify_all()

    def state(self, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        if not self.settings["enabled"]:
            return "disabled"
        return "armed" if self.classifier.armed(now) else "idle"

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state(),
            "settings": dict(self.settings),
            "armed_until": round(self.classifier.armed_until, 2)
            if self.classifier.armed_until else None,
            "last_event": self.last_event,
            "counts": dict(self.counts),
            "pose_available": self.pose_service.office_occupied() is not None,
            "occupied": self.pose_service.office_occupied(),
            "gestures": {name: action
                         for name, action in GESTURE_ACTIONS.items()},
        }

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(values, dict) or \
                set(values) - set(DEFAULT_SETTINGS):
            raise ValueError("Unsupported gesture setting")
        merged = {**self.settings, **values}
        for name in ("enabled", "require_arm", "mirror"):
            if name in merged and not isinstance(merged[name], bool):
                raise ValueError(f"{name} must be boolean")
        for name, (low, high) in _SETTING_BOUNDS.items():
            value = merged.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}")
        self.settings = merged
        self.classifier.settings = merged
        if self.store:
            self.store.save_monitor_state(SETTINGS_KEY, merged)
        return dict(self.settings)

    async def events(self):
        """Yield each classified gesture event (PoseService.results() shape)."""
        await self.start()
        generation = self._generation
        while True:
            async with self._condition:
                await self._condition.wait_for(
                    lambda: self._generation != generation
                    or (self._task and self._task.done()))
                changed = self._generation != generation
                done = bool(self._task and self._task.done())
                generation = self._generation
            if changed and self.last_event is not None:
                yield self.last_event
            if done:
                return
