"""ADA-0512: LINE lanes get the clip link, not the file.

A tool result carrying video_url must reach LINE as a text message
holding the public URL — never a videoMessage upload (size cap /
re-encode). TG lanes keep file delivery via their own relay.
"""
import asyncio
import unittest

from backend import line_relay


class VideoLinkPolicy(unittest.TestCase):

    def _relay(self):
        return line_relay.LineRelay()

    def test_media_msgs_video_returns_text_link(self):
        relay = self._relay()
        msgs = asyncio.run(relay._media_msgs(
            "https://edge.example/relay-img/arch/x.mp4", "my clip",
            "video"))
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["type"], "text")
        self.assertIn("https://edge.example/relay-img/arch/x.mp4",
                      msgs[0]["text"])
        self.assertIn("my clip", msgs[0]["text"])

    def test_relative_url_resolved_against_lane_base(self):
        relay = self._relay()
        msgs = asyncio.run(relay._media_msgs("/v1/media/x.mp4", "",
                                             "video"))
        self.assertEqual(msgs[0]["type"], "text")
        self.assertTrue(msgs[0]["text"].startswith("http"),
                        msgs[0]["text"])
        self.assertIn("/v1/media/x.mp4", msgs[0]["text"])

    def test_images_from_still_detects_video_kind(self):
        # detection unchanged — the link policy lives at send time
        triples = line_relay._images_from(
            [{"video_url": "https://x/y.mp4", "title": "t"}])
        self.assertEqual(triples[0][2], "video")


if __name__ == "__main__":
    unittest.main()
