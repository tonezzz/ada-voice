"""text-lane-provider-routing (2026-10-23): relay /ws sessions
(channel=telegram|line) must get the REST text provider — never a
billed Gemini Live session for text-only use."""
import unittest
from unittest.mock import patch

import pwa_server
from backend.openrouter_provider import OpenRouterTextProvider


def _wants(mode="", channel=None, caller=None):
    return pwa_server._wants_text_provider(mode, channel, caller)


class RoutingHelperTests(unittest.TestCase):
    def test_relay_channels_route_to_text(self):
        for ch in ("telegram", "tg", "line"):
            self.assertTrue(_wants("", ch, None), ch)

    def test_voice_and_other_channels_stay_live(self):
        for ch in (None, "", "voice", "chat", "web"):
            self.assertFalse(_wants("", ch, None), ch)

    def test_mode_text_forces_text_anywhere(self):
        self.assertTrue(_wants("text", "voice", None))
        self.assertTrue(_wants("text", None, None))

    def test_mode_live_overrides_channel_routing(self):
        for ch in ("telegram", "tg", "line"):
            self.assertFalse(_wants("live", ch, None), ch)

    def test_mode_live_does_not_override_callers_list(self):
        with patch.object(pwa_server, "_TEXT_ONLY_CALLERS", {"relay-bot"}):
            self.assertTrue(_wants("live", "line", "relay-bot"))
            self.assertTrue(_wants("", None, "relay-bot"))

    def test_channel_set_env_override_disables_routing(self):
        with patch.object(pwa_server, "_TEXT_ONLY_CHANNELS", set()):
            self.assertFalse(_wants("", "line", None))
            self.assertFalse(_wants("", "telegram", None))

    def test_custom_channel_set(self):
        with patch.object(
                pwa_server, "_TEXT_ONLY_CHANNELS", {"line", "chat"}):
            self.assertTrue(_wants("", "chat", None))
            self.assertFalse(_wants("", "telegram", None))


class TextProviderWatchdogSurfaceTests(unittest.TestCase):
    """stall_watchdog probes every session — a REST text provider must
    answer them or the watchdog task dies on the first iteration."""

    def test_watchdog_probes_healthy(self):
        p = OpenRouterTextProvider(session_id="t", channel="line")
        self.assertFalse(p.upstream_dead())
        self.assertIsNone(p.upstream_close_code())
        self.assertFalse(p.is_stalled())
        self.assertIsNone(p.resumption_handle)


if __name__ == "__main__":
    unittest.main()
