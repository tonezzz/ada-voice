"""YOLO-pose gesture controls — wave to arm, then swipe / palm-hold / drop.

Consumes the shared pose stream (PoseService.results(), which runs the
Hailo yolov8s_pose network with the CPU MoveNet fallback) and turns wrist
trajectories into kiosk commands fanned out on /ws/gesture:

    wave        — arms the controller (idle -> armed)
    swipe_left  — while armed, fast hand travel left (panel navigation)
    swipe_right — while armed, fast hand travel right (panel navigation)
    palm_hold   — while armed, a raised hand held still (settings drawer)
    drop        — while armed, arm dropped fast (dismiss + disarm)

Only body keypoints are available (no hand landmarks), so every gesture is
defined on wrist/shoulder trajectories. The camera's 180° mount rotation
makes the stream read like a selfie view, so positive dx is the user's
perceived rightward motion. The controller is intentionally a pure state
machine: callers feed pose results plus a monotonic timestamp and receive
emitted events, which keeps it deterministic under test.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from datetime import datetime
from typing import Any

logger = logging.getLogger("voice.gestures")

WRIST_FOR_HAND = {"left": "left_wrist", "right": "right_wrist"}
SHOULDER_FOR_HAND = {"left": "left_shoulder", "right": "right_shoulder"}
HANDS = ("left", "right")

CONFIDENCE = 0.30
# Wrist must be this far above its shoulder (normalized y grows downward)
# to count as "raised" — the gate for wave and palm_hold.
RAISE_MARGIN = 0.02
RAISE_DROP_HYSTERESIS = 0.08
# Wave: raised wrist oscillating horizontally — >=2 direction reversals
# with enough total travel inside the window.
WAVE_WINDOW = 2.2
WAVE_MIN_TRAVEL = 0.18
WAVE_REVERSALS = 2
REVERSAL_MIN = 0.035
WAVE_RAISED_RATIO = 0.6
# While armed: fast mostly-horizontal wrist travel.
SWIPE_DX = 0.18
SWIPE_WINDOW = 0.9
SWIPE_RATIO = 1.6
SWIPE_MONOTONY = 1.5
SWIPE_COOLDOWN = 0.9
# While armed: raised wrist held nearly still.
HOLD_SECONDS = 1.1
HOLD_TOLERANCE = 0.06
HOLD_MIN_SAMPLES = 3
# While armed: fast downward wrist travel dismisses and disarms.
DROP_DY = 0.20
DROP_WINDOW = 0.7
DROP_RATIO = 1.6
ARM_TIMEOUT = 12.0
EVENT_COOLDOWN = 0.5
# Wrist unseen longer than this discards its trajectory.
SAMPLE_GAP = 1.0
SAMPLE_KEEP = 3.0

GESTURE_EVENTS = ("wave", "swipe_left", "swipe_right", "palm_hold", "drop")


def _points(result: dict[str, Any]) -> dict[str, dict[str, float]]:
    return {p["name"]: p for p in result.get("keypoints", []) if isinstance(p, dict)}


def _confident(point: dict[str, float] | None, threshold: float = CONFIDENCE) -> bool:
    return bool(point) and float(point.get("score", 0)) >= threshold


class _Hand:
    """One wrist's recent trajectory plus per-hand detection latches."""

    __slots__ = ("samples", "raised_since", "hold_fired", "swipe_cooldown_until")

    def __init__(self) -> None:
        self.samples: deque[tuple[float, float, float, bool]] = deque()
        self.raised_since: float | None = None
        self.hold_fired = False
        self.swipe_cooldown_until = 0.0


class GestureController:
    """Temporal gesture state machine fed by shared pose results."""

    def __init__(self, arm_timeout: float = ARM_TIMEOUT, enabled: bool = True) -> None:
        self.enabled = enabled
        self.arm_timeout = arm_timeout
        self.state = "idle"
        self.armed_until: float | None = None
        self._hands = {hand: _Hand() for hand in HANDS}
        self._event_cooldown_until = 0.0
        self.last_event: dict[str, Any] | None = None

    def _event(self, name: str, hand: str, now: float, **detail: Any) -> dict[str, Any]:
        self._event_cooldown_until = now + EVENT_COOLDOWN
        event = {
            "name": name, "hand": hand,
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            **detail,
        }
        self.last_event = event
        return event

    def _rearm(self, now: float) -> None:
        self.armed_until = now + self.arm_timeout

    def _prune(self, hand_state: _Hand, now: float) -> None:
        samples = hand_state.samples
        while samples and now - samples[0][0] > SAMPLE_KEEP:
            samples.popleft()
        if samples and now - samples[-1][0] > SAMPLE_GAP:
            samples.clear()
            hand_state.raised_since = None

    def _raised(self, point: dict[str, float] | None, shoulder: dict[str, float] | None) -> bool | None:
        """True/False when decidable, None when the shoulder is missing."""
        if not _confident(point) or not _confident(shoulder):
            return None
        return float(point["y"]) < float(shoulder["y"]) - RAISE_MARGIN

    def _is_wave(self, hand_state: _Hand, now: float) -> bool:
        samples = [s for s in hand_state.samples if now - s[0] <= WAVE_WINDOW]
        if len(samples) < 4:
            return False
        if sum(1 for s in samples if s[3]) < len(samples) * WAVE_RAISED_RATIO:
            return False
        travel, reversals, run_sign, run_length = 0.0, 0, 0, 0.0
        for (_, prev_x, _, _), (_, x, _, _) in zip(samples, samples[1:]):
            dx = x - prev_x
            travel += abs(dx)
            if not dx:
                continue
            sign = 1 if dx > 0 else -1
            if sign == run_sign:
                run_length += abs(dx)
                continue
            if run_sign and run_length >= REVERSAL_MIN:
                reversals += 1
            run_sign, run_length = sign, abs(dx)
        return reversals >= WAVE_REVERSALS and travel >= WAVE_MIN_TRAVEL

    def _window_delta(self, hand_state: _Hand, now: float, window: float) -> tuple[float, float, float] | None:
        samples = [s for s in hand_state.samples if now - s[0] <= window]
        if len(samples) < 2:
            return None
        first, last = samples[0], samples[-1]
        variation = sum(abs(b[1] - a[1]) for a, b in zip(samples, samples[1:]))
        return last[1] - first[1], last[2] - first[2], variation

    def process(self, result: dict[str, Any], now: float | None = None) -> list[dict[str, Any]]:
        """Feed one pose result; return any gesture events it produced."""
        now = time.monotonic() if now is None else now
        if not self.enabled:
            return []
        events: list[dict[str, Any]] = []
        points = _points(result)
        if self.state == "armed" and self.armed_until is not None and now > self.armed_until:
            self.state = "idle"
            self.armed_until = None
            for hand_state in self._hands.values():
                hand_state.hold_fired = False
            events.append(self._event("disarm", "", now))
        for hand in HANDS:
            hand_state = self._hands[hand]
            wrist = points.get(WRIST_FOR_HAND[hand])
            shoulder = points.get(SHOULDER_FOR_HAND[hand])
            raised = self._raised(wrist, shoulder)
            self._prune(hand_state, now)
            if _confident(wrist):
                hand_state.samples.append((now, float(wrist["x"]), float(wrist["y"]), bool(raised)))
            if raised is True and hand_state.raised_since is None:
                hand_state.raised_since = now
            elif raised is False and hand_state.raised_since is not None:
                if float(wrist["y"]) > float(shoulder["y"]) + RAISE_DROP_HYSTERESIS:
                    hand_state.raised_since = None
                    hand_state.hold_fired = False
            if now < self._event_cooldown_until:
                continue
            if self.state == "idle":
                if self._is_wave(hand_state, now):
                    self.state = "armed"
                    self._rearm(now)
                    events.append(self._event("wave", hand, now))
                continue
            delta = self._window_delta(hand_state, now, DROP_WINDOW)
            if delta and delta[1] >= DROP_DY and delta[1] >= DROP_RATIO * abs(delta[0]):
                self.state = "idle"
                self.armed_until = None
                hand_state.samples.clear()
                hand_state.hold_fired = False
                events.append(self._event("drop", hand, now, dy=round(delta[1], 3)))
                continue
            if raised is True and hand_state.raised_since is not None and not hand_state.hold_fired:
                held = [s for s in hand_state.samples if now - s[0] <= HOLD_SECONDS]
                if (now - hand_state.raised_since >= HOLD_SECONDS
                        and len(held) >= HOLD_MIN_SAMPLES
                        and max(s[1] for s in held) - min(s[1] for s in held) <= HOLD_TOLERANCE
                        and max(s[2] for s in held) - min(s[2] for s in held) <= HOLD_TOLERANCE):
                    hand_state.hold_fired = True
                    self._rearm(now)
                    events.append(self._event("palm_hold", hand, now))
                    continue
            if now < hand_state.swipe_cooldown_until:
                continue
            delta = self._window_delta(hand_state, now, SWIPE_WINDOW)
            if delta and abs(delta[0]) >= SWIPE_DX and abs(delta[0]) >= SWIPE_RATIO * abs(delta[1]) \
                    and delta[2] <= SWIPE_MONOTONY * abs(delta[0]):
                name = "swipe_right" if delta[0] > 0 else "swipe_left"
                hand_state.swipe_cooldown_until = now + SWIPE_COOLDOWN
                self._rearm(now)
                events.append(self._event(name, hand, now, dx=round(delta[0], 3)))
        return events

    def status(self, now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        return {
            "enabled": self.enabled,
            "state": self.state,
            "armed_remaining_s": round(max(0.0, self.armed_until - now), 1) if self.armed_until else 0.0,
            "last_event": self.last_event,
        }


class GestureService:
    """Feed the controller from the shared pose stream and fan out events."""

    def __init__(self, pose_service: Any, store: Any = None, enabled: bool | None = None) -> None:
        if enabled is None:
            saved = store.monitor_state("gesture_controls_settings") if store is not None else {}
            if "enabled" in saved:
                enabled = bool(saved["enabled"])
            else:
                enabled = os.environ.get("ADA_GESTURES_ENABLED", "true").lower() not in {"0", "false", "no"}
        self.pose_service, self.store = pose_service, store
        self.controller = GestureController(enabled=bool(enabled))
        self._task: asyncio.Task[None] | None = None
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._last_published_state = self.controller.state

    @property
    def enabled(self) -> bool:
        return self.controller.enabled

    def status(self) -> dict[str, Any]:
        return {"type": "gesture_status", **self.controller.status()}

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        self.controller.enabled = bool(enabled)
        if not enabled:
            self.controller.state = "idle"
            self.controller.armed_until = None
            if self._task:
                self._task.cancel()
                self._task = None
        else:
            self._ensure_task()
        if self.store is not None:
            saved = self.store.monitor_state("gesture_controls_settings")
            saved["enabled"] = bool(enabled)
            self.store.save_monitor_state("gesture_controls_settings", saved)
        self._publish(self.status())
        return self.controller.status()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=30)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def _publish(self, message: dict[str, Any]) -> None:
        for queue in self._subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    def _ensure_task(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def start(self) -> None:
        if self.enabled:
            self._ensure_task()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        """Subscribe to the shared pose stream; restart it if the loop dies."""
        while self.enabled:
            try:
                async for result, _frame in self.pose_service.results():
                    if not self.enabled:
                        break
                    self._feed(result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("gesture stream interrupted: %s", exc)
            if self.enabled:
                await asyncio.sleep(1)

    def _feed(self, result: dict[str, Any]) -> None:
        if not self.enabled:
            return
        events = self.controller.process(result)
        for event in events:
            self._publish({"type": "gesture_event", **event})
        if events or self.controller.state != self._last_published_state:
            self._last_published_state = self.controller.state
            self._publish(self.status())
