import asyncio
import unittest

from backend.gesture_controls import GestureController, GestureService


def frame(**points):
    """Build a pose result. Each arg is name=(x, y, score)."""
    return {"keypoints": [
        {"name": name, "x": xy[0], "y": xy[1], "score": xy[2]}
        for name, xy in points.items()
    ]}


def person(right_wrist=(0.5, 0.75, 0.9), left_wrist=(0.5, 0.75, 0.0)):
    """Seated person: shoulders high-confidence, wrists at rest by default."""
    return frame(
        nose=(0.5, 0.30, 0.9),
        left_shoulder=(0.42, 0.45, 0.9),
        right_shoulder=(0.58, 0.45, 0.9),
        left_wrist=left_wrist,
        right_wrist=right_wrist,
    )


def feed_wave(controller, hand="right", t=0.0, score=0.9):
    """Oscillate a raised wrist; returns (events, next_t)."""
    wrist = f"{hand}_wrist"
    events = []
    for i, x in enumerate((0.45, 0.68, 0.45, 0.68)):
        now = t + i * 0.2
        events += controller.process(person(**{wrist: (x, 0.25, score)}), now=now)
    return events, t + 0.6


def arm(controller, t=0.0):
    events, t = feed_wave(controller, t=t)
    assert any(e["name"] == "wave" for e in events), events
    assert controller.state == "armed"
    return t


class GestureControllerTests(unittest.TestCase):
    def test_wave_arms_controller(self) -> None:
        controller = GestureController()
        events, _ = feed_wave(controller)
        self.assertEqual([e["name"] for e in events], ["wave"])
        self.assertEqual(events[0]["hand"], "right")
        self.assertEqual(controller.state, "armed")
        self.assertEqual(controller.last_event["name"], "wave")

    def test_left_hand_also_arms(self) -> None:
        controller = GestureController()
        events, _ = feed_wave(controller, hand="left")
        self.assertEqual([e["name"] for e in events], ["wave"])
        self.assertEqual(events[0]["hand"], "left")

    def test_wave_requires_raised_wrist(self) -> None:
        controller = GestureController()
        events = []
        for i, x in enumerate((0.45, 0.68, 0.45, 0.68)):
            events += controller.process(
                person(right_wrist=(x, 0.70, 0.9)), now=i * 0.2)
        self.assertEqual(events, [])
        self.assertEqual(controller.state, "idle")

    def test_wave_requires_oscillation(self) -> None:
        controller = GestureController()
        events = []
        for i, x in enumerate((0.40, 0.50, 0.60, 0.70)):
            events += controller.process(
                person(right_wrist=(x, 0.25, 0.9)), now=i * 0.2)
        self.assertEqual(events, [])
        self.assertEqual(controller.state, "idle")

    def test_low_confidence_keypoints_are_ignored(self) -> None:
        controller = GestureController()
        events, _ = feed_wave(controller, score=0.1)
        self.assertEqual(events, [])
        self.assertEqual(controller.state, "idle")

    def test_swipe_right_while_armed(self) -> None:
        controller = GestureController()
        t = arm(controller)
        events = []
        for i, x in enumerate((0.45, 0.56, 0.68), start=1):
            events += controller.process(
                person(right_wrist=(x, 0.55, 0.9)), now=t + EVENT_COOLDOWN_GAP + i * 0.15)
        self.assertEqual([e["name"] for e in events], ["swipe_right"])
        self.assertEqual(controller.state, "armed")

    def test_swipe_left_while_armed(self) -> None:
        controller = GestureController()
        t = arm(controller)
        events = []
        for i, x in enumerate((0.68, 0.56, 0.45), start=1):
            events += controller.process(
                person(right_wrist=(x, 0.55, 0.9)), now=t + EVENT_COOLDOWN_GAP + i * 0.15)
        self.assertEqual([e["name"] for e in events], ["swipe_left"])

    def test_swipe_ignored_while_idle(self) -> None:
        controller = GestureController()
        events = []
        for i, x in enumerate((0.45, 0.56, 0.68)):
            events += controller.process(
                person(right_wrist=(x, 0.55, 0.9)), now=i * 0.15)
        self.assertEqual(events, [])

    def test_swipe_requires_horizontal_dominance(self) -> None:
        controller = GestureController()
        t = arm(controller)
        events = []
        # dx=.21 exceeds the distance gate but the dy=.14 drift keeps the
        # 1.6x horizontal-dominance ratio from holding — and stays under
        # the drop threshold so nothing else fires either.
        for i, (x, y) in enumerate(((0.40, 0.30), (0.50, 0.37), (0.61, 0.44)), start=1):
            events += controller.process(
                person(right_wrist=(x, y, 0.9)), now=t + EVENT_COOLDOWN_GAP + i * 0.15)
        self.assertEqual(events, [])

    def test_swipe_cooldown_blocks_immediate_reversal(self) -> None:
        controller = GestureController()
        t = arm(controller)
        now = t + EVENT_COOLDOWN_GAP
        first = []
        for i, x in enumerate((0.45, 0.56, 0.68)):
            first += controller.process(
                person(right_wrist=(x, 0.55, 0.9)), now=now + i * 0.15)
        self.assertEqual([e["name"] for e in first], ["swipe_right"])
        now += 0.5
        second = []
        for i, x in enumerate((0.68, 0.56, 0.45)):
            second += controller.process(
                person(right_wrist=(x, 0.55, 0.9)), now=now + i * 0.15)
        self.assertEqual(second, [])

    def test_palm_hold_fires_once_per_raise(self) -> None:
        controller = GestureController()
        t = arm(controller)
        events = []
        now = t + EVENT_COOLDOWN_GAP
        for i in range(10):
            events += controller.process(
                person(right_wrist=(0.55, 0.25, 0.9)), now=now + i * 0.2)
        names = [e["name"] for e in events]
        self.assertEqual(names, ["palm_hold"])
        # Keep holding — the latch blocks a duplicate.
        more = controller.process(
            person(right_wrist=(0.55, 0.25, 0.9)), now=now + 2.2)
        self.assertEqual(more, [])

    def test_palm_hold_rear_after_lower_and_reraise(self) -> None:
        controller = GestureController()
        t = arm(controller)
        now = t + EVENT_COOLDOWN_GAP
        for i in range(8):
            controller.process(
                person(right_wrist=(0.55, 0.25, 0.9)), now=now + i * 0.2)
        now += 1.8
        # Lower gradually so the drop detector does not fire.
        for i, y in enumerate((0.33, 0.41, 0.49, 0.57, 0.65)):
            controller.process(
                person(right_wrist=(0.55, y, 0.9)), now=now + i * 0.4)
        self.assertEqual(controller.state, "armed")
        now += 2.0
        events = []
        for i in range(8):
            events += controller.process(
                person(right_wrist=(0.55, 0.25, 0.9)), now=now + i * 0.2)
        self.assertIn("palm_hold", [e["name"] for e in events])

    def test_drop_dismisses_and_disarms(self) -> None:
        controller = GestureController()
        t = arm(controller)
        now = t + EVENT_COOLDOWN_GAP
        controller.process(person(right_wrist=(0.55, 0.30, 0.9)), now=now)
        events = controller.process(
            person(right_wrist=(0.55, 0.62, 0.9)), now=now + 0.3)
        self.assertEqual([e["name"] for e in events], ["drop"])
        self.assertEqual(controller.state, "idle")
        followup = controller.process(
            person(right_wrist=(0.75, 0.62, 0.9)), now=now + 0.5)
        self.assertEqual(followup, [])

    def test_drop_ignored_while_idle(self) -> None:
        controller = GestureController()
        controller.process(person(right_wrist=(0.55, 0.30, 0.9)), now=0.0)
        events = controller.process(
            person(right_wrist=(0.55, 0.62, 0.9)), now=0.3)
        self.assertEqual(events, [])

    def test_arm_timeout_disarms(self) -> None:
        controller = GestureController(arm_timeout=1.0)
        t = arm(controller)
        events = controller.process(person(), now=t + 1.2)
        self.assertEqual([e["name"] for e in events], ["disarm"])
        self.assertEqual(controller.state, "idle")
        self.assertEqual(controller.status(now=t + 1.2)["armed_remaining_s"], 0.0)

    def test_disabled_controller_is_silent(self) -> None:
        controller = GestureController(enabled=False)
        events, _ = feed_wave(controller)
        self.assertEqual(events, [])
        self.assertEqual(controller.state, "idle")

    def test_status_shape(self) -> None:
        controller = GestureController()
        status = controller.status()
        self.assertEqual(
            set(status), {"enabled", "state", "armed_remaining_s", "last_event"})
        self.assertTrue(status["enabled"])
        self.assertEqual(status["state"], "idle")


EVENT_COOLDOWN_GAP = 1.0  # > 0.5s event cooldown and > the 0.9s swipe window


class _Store:
    def __init__(self) -> None:
        self.saved: dict[str, dict] = {}

    def monitor_state(self, key):
        return dict(self.saved.get(key, {}))

    def save_monitor_state(self, key, state):
        self.saved[key] = dict(state)


class _FakePose:
    def __init__(self, results):
        self._results = results

    async def results(self):
        for result in self._results:
            yield result, b"jpeg"


class GestureServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_feed_publishes_event_and_status(self) -> None:
        service = GestureService(_FakePose([]), enabled=True)
        queue = service.subscribe()
        for i, x in enumerate((0.45, 0.68, 0.45, 0.68)):
            service._feed(person(right_wrist=(x, 0.25, 0.9)))
        messages = [queue.get_nowait() for _ in range(queue.qsize())]
        kinds = [m["type"] for m in messages]
        self.assertIn("gesture_event", kinds)
        self.assertIn("gesture_status", kinds)
        event = next(m for m in messages if m["type"] == "gesture_event")
        self.assertEqual(event["name"], "wave")
        status = [m for m in messages if m["type"] == "gesture_status"][-1]
        self.assertEqual(status["state"], "armed")
        service.unsubscribe(queue)

    async def test_set_enabled_persists_and_resets_state(self) -> None:
        store = _Store()
        service = GestureService(_FakePose([]), store=store, enabled=True)
        queue = service.subscribe()
        service._feed(person(right_wrist=(0.68, 0.25, 0.9)))
        service.set_enabled(False)
        self.assertFalse(service.enabled)
        self.assertEqual(store.saved["gesture_controls_settings"]["enabled"], False)
        self.assertEqual(service.status()["state"], "idle")
        service._feed(person(right_wrist=(0.45, 0.25, 0.9)))
        self.assertEqual(queue.get_nowait()["type"], "gesture_status")
        self.assertTrue(queue.empty())
        service.unsubscribe(queue)

    async def test_enabled_reads_saved_settings(self) -> None:
        store = _Store()
        store.saved["gesture_controls_settings"] = {"enabled": False}
        service = GestureService(_FakePose([]), store=store)
        self.assertFalse(service.enabled)

    async def test_disabled_feed_is_silent(self) -> None:
        service = GestureService(_FakePose([]), enabled=False)
        queue = service.subscribe()
        service._feed(person(right_wrist=(0.68, 0.25, 0.9)))
        self.assertTrue(queue.empty())
        service.unsubscribe(queue)


if __name__ == "__main__":
    unittest.main()
