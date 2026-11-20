import asyncio
import tempfile
import unittest
from pathlib import Path

from backend.gesture_controls import (
    GESTURE_ACTIONS,
    GestureClassifier,
    GestureControlService,
)
from backend.posture import PostureStore


def kp(x, y, score=0.9, wrist="right_wrist"):
    """A minimal pose result: both shoulders + nose + one wrist."""
    return [
        {"name": "nose", "x": 0.5, "y": 0.22, "score": 0.9},
        {"name": "left_shoulder", "x": 0.40, "y": 0.45, "score": 0.9},
        {"name": "right_shoulder", "x": 0.60, "y": 0.45, "score": 0.9},
        {"name": wrist, "x": x, "y": y, "score": score},
    ]


def raised(x):       # wrist above the shoulder line (hand up)
    return kp(x, 0.30)


def lowered(x):      # wrist below the shoulder line (hand down)
    return kp(x, 0.60)


def feed_series(clf, frames):
    """Feed [(t, keypoints)] frames; return emitted events."""
    events = []
    for t, frame in frames:
        event = clf.feed(t, frame)
        if event:
            events.append(event)
    return events


def wave_frames(start, step=0.3):
    """Raised hand oscillating — always a clean wave."""
    return [(start + i * step, raised(x))
            for i, x in enumerate((0.50, 0.62, 0.50, 0.62))]


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.clf = GestureClassifier()

    def arm(self, t=0.0):
        """Drive a wave so the classifier is armed; return last frame time."""
        events = feed_series(self.clf, wave_frames(t))
        self.assertTrue(any(e["gesture"] == "wave" for e in events))
        self.assertTrue(self.clf.armed(t + 0.9))
        return t + 0.9

    def test_wave_arms_controller(self):
        t = self.arm()
        self.assertGreater(self.clf.armed_until, t)

    def test_wave_requires_reversals(self):
        # One-direction drift must not count as a wave.
        events = feed_series(self.clf, [
            (0.0, raised(0.40)), (0.3, raised(0.55)),
            (0.6, raised(0.70)), (0.9, raised(0.85)),
        ])
        self.assertFalse(any(e["gesture"] == "wave" for e in events))
        self.assertFalse(self.clf.armed(1.0))

    def test_require_arm_blocks_swipe(self):
        events = feed_series(self.clf, [
            (0.0, raised(0.30)), (0.3, raised(0.40)),
            (0.6, raised(0.55)),
        ])
        self.assertEqual(events, [])

    def test_swipe_reports_user_space_direction(self):
        t = self.arm()
        # dx +.38 in image space; mirror=True -> user sees "left". The
        # first frames fall inside the post-wave grace period by design.
        events = feed_series(self.clf, [
            (t + 1.1, raised(0.30)), (t + 1.4, raised(0.42)),
            (t + 1.7, raised(0.55)), (t + 2.0, raised(0.68)),
        ])
        swipes = [e for e in events if e["gesture"] == "swipe"]
        self.assertEqual(len(swipes), 1)
        self.assertEqual(swipes[0]["action"], "nav")
        self.assertEqual(swipes[0]["direction"], "left")

    def test_swipe_direction_without_mirror(self):
        self.clf.settings = {**self.clf.settings, "mirror": False}
        t = self.arm()
        events = feed_series(self.clf, [
            (t + 1.1, raised(0.30)), (t + 1.4, raised(0.42)),
            (t + 1.7, raised(0.55)), (t + 2.0, raised(0.68)),
        ])
        swipes = [e for e in events if e["gesture"] == "swipe"]
        self.assertEqual(swipes[0]["direction"], "right")

    def test_post_wave_grace_suppresses_settle_swipe(self):
        # Hand travelling back to rest right after a wave must not
        # register as a navigation swipe inside arm_grace_s.
        t = self.arm()
        events = feed_series(self.clf, [
            (t + 1.0, raised(0.40)), (t + 1.3, raised(0.30)),
        ])
        self.assertFalse(any(e["gesture"] == "swipe" for e in events))

    def test_palm_hold_confirms_once_until_lowered(self):
        # Isolate the hold-lock from arming by requiring no arm.
        self.clf.settings = {**self.clf.settings, "require_arm": False}
        steady = [(i * 0.3, raised(0.50)) for i in range(7)]
        events = feed_series(self.clf, steady)
        holds = [e for e in events if e["gesture"] == "palm_hold"]
        self.assertEqual(len(holds), 1)
        self.assertEqual(holds[0]["action"], "confirm")
        # Still holding — no refire.
        more = feed_series(
            self.clf, [(2.2 + i * 0.3, raised(0.50)) for i in range(6)])
        self.assertFalse(any(e["gesture"] == "palm_hold" for e in more))
        # Lower the hand -> lock clears (a drop may also fire; harmless).
        feed_series(self.clf, [(4.5, lowered(0.50))])
        again = feed_series(
            self.clf, [(5.0 + i * 0.3, raised(0.50)) for i in range(7)])
        self.assertTrue(any(e["gesture"] == "palm_hold" for e in again))

    def test_drop_dismisses_and_disarms(self):
        t = self.arm()
        events = feed_series(self.clf, [
            (t + 1.0, raised(0.50)), (t + 1.3, lowered(0.50)),
        ])
        drops = [e for e in events if e["gesture"] == "drop"]
        self.assertEqual(len(drops), 1)
        self.assertEqual(drops[0]["action"], "dismiss")
        self.assertFalse(self.clf.armed(t + 1.4))

    def test_arm_expires_without_gesture(self):
        t = self.arm()
        # No gesture for armed_ttl_s (12s) — swipe must not fire.
        events = feed_series(self.clf, [
            (t + 13.0, raised(0.30)), (t + 13.3, raised(0.45)),
            (t + 13.6, raised(0.60)),
        ])
        self.assertFalse(any(e["gesture"] == "swipe" for e in events))

    def test_disabled_classifier_is_silent(self):
        self.clf.settings = {**self.clf.settings, "enabled": False}
        events = feed_series(self.clf, wave_frames(0.0))
        self.assertEqual(events, [])

    def test_low_score_wrist_ignored(self):
        events = feed_series(self.clf, [
            (t, kp(x, 0.30, score=0.10))
            for t, x in ((0.0, 0.50), (0.3, 0.62), (0.6, 0.50), (0.9, 0.62))
        ])
        self.assertEqual(events, [])

    def test_cooldown_blocks_refire(self):
        t = self.arm()
        first = feed_series(self.clf, [
            (t + 1.0, raised(0.30)), (t + 1.3, raised(0.45)),
            (t + 1.6, raised(0.60)), (t + 1.9, raised(0.75)),
        ])
        second = feed_series(self.clf, [
            (t + 2.2, raised(0.30)), (t + 2.5, raised(0.45)),
            (t + 2.9, raised(0.60)),
        ])
        self.assertEqual(sum(e["gesture"] == "swipe" for e in first), 1)
        self.assertFalse(any(e["gesture"] == "swipe" for e in second))

    def test_raise_fallback_without_shoulders(self):
        # No shoulder keypoints -> raise_fallback_y (0.42) decides raised.
        def no_shoulders(x):
            return [{"name": "right_wrist", "x": x, "y": 0.30, "score": 0.9}]
        events = feed_series(self.clf, [
            (t, no_shoulders(x))
            for t, x in ((0.0, 0.50), (0.3, 0.62), (0.6, 0.50), (0.9, 0.62))
        ])
        self.assertTrue(any(e["gesture"] == "wave" for e in events))


class FakePose:
    def __init__(self, frames):
        self._frames = frames
        self.latest_result = None

    def office_occupied(self):
        return True

    async def results(self):
        for frame in self._frames:
            yield {"keypoints": frame}, b"jpeg"


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PostureStore(Path(self.tmp.name) / "gestures.db")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_service_emits_classified_events(self):
        frames = [frame for _, frame in wave_frames(0.0)]
        service = GestureControlService(FakePose(frames), self.store)
        await service.start()
        for _ in range(50):
            if service.last_event:
                break
            await asyncio.sleep(0.01)
        self.assertIsNotNone(service.last_event)
        self.assertEqual(service.last_event["gesture"], "wave")
        self.assertEqual(service.counts["wave"], 1)
        self.assertEqual(service.state(), "armed")
        status = service.status()
        self.assertEqual(status["gestures"], GESTURE_ACTIONS)
        await service.stop()

    async def test_update_settings_validates_and_persists(self):
        service = GestureControlService(FakePose([]), self.store)
        with self.assertRaises(ValueError):
            service.update_settings({"nonsense": 1})
        with self.assertRaises(ValueError):
            service.update_settings({"enabled": "yes"})
        with self.assertRaises(ValueError):
            service.update_settings({"swipe_frac": 99})
        merged = service.update_settings({"enabled": False, "swipe_frac": 0.3})
        self.assertFalse(merged["enabled"])
        self.assertEqual(merged["swipe_frac"], 0.3)
        saved = self.store.monitor_state("gesture_controls_settings")
        self.assertFalse(saved["enabled"])
        service2 = GestureControlService(FakePose([]), self.store)
        self.assertFalse(service2.settings["enabled"])

    async def test_events_generator_fans_out(self):
        frames = [frame for _, frame in wave_frames(0.0)]
        service = GestureControlService(FakePose(frames), self.store)
        events = service.events()
        event = await asyncio.wait_for(anext(events), timeout=2)
        self.assertEqual(event["gesture"], "wave")
        await service.stop()


if __name__ == "__main__":
    unittest.main()
