"""ADA-0512 hybrid delivery: LINE lanes get poster jpeg + playable link.

A tool result carrying video_url must reach LINE as an image bubble
(the clip's first frame, hosted on the /relay-img nonce lane) plus a
text message holding a playable URL — never a videoMessage upload
(size cap / re-encode). A tailnet-only source URL (*.ts.net, CGNAT,
RFC1918) is re-hosted under the durable /relay-img/arch/video lane so
members off-tailnet can still play it. Fetch/poster failures degrade
to the bare link text. TG lanes keep file delivery via their own relay.
"""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

from backend import line_relay

_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4000
_POSTER = b"\xff\xd8\xffposter-frame"


class _Resp:
    def __init__(self, status=200, content=_MP4, content_type="video/mp4"):
        self.status_code = status
        self.content = content
        self.headers = {"content-type": content_type}


class _FakeHttp:
    """Minimal httpx.AsyncClient stand-in — records GETs, returns a
    canned clip; errors/None emulate an unreachable source."""

    def __init__(self, resp=None, exc=None):
        self.resp = resp if resp is not None else _Resp()
        self.exc = exc
        self.got = []

    async def get(self, url, timeout=None, follow_redirects=None):
        self.got.append(url)
        if self.exc:
            raise self.exc
        return self.resp


class TailnetOnlyUrl(unittest.TestCase):

    def test_ts_net_and_private_hosts(self):
        for u in ("https://tony-dell.taila0626a.ts.net/api/input-bridge/"
                  "frame?screen=0&token=render:x",
                  "http://100.64.1.2:11023/v1/media/x.mp4",
                  "http://192.168.1.5:8000/x.mp4",
                  "http://127.0.0.1:8000/x.mp4",
                  "http://localhost/x.mp4"):
            self.assertTrue(line_relay._tailnet_only_url(u), u)

    def test_public_hosts(self):
        for u in ("https://157.85.102.125.sslip.io/relay-img/arch/x.mp4",
                  "https://edge.example/relay-img/arch/x.mp4"):
            self.assertFalse(line_relay._tailnet_only_url(u), u)


class VideoLinkPolicy(unittest.TestCase):

    def _relay(self, http=None):
        relay = line_relay.LineRelay()
        relay.http = http
        return relay

    def test_fetch_failure_degrades_to_text_link(self):
        relay = self._relay(_FakeHttp(exc=RuntimeError("down")))
        msgs = asyncio.run(relay._media_msgs(
            "https://edge.example/relay-img/arch/x.mp4", "my clip",
            "video"))
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["type"], "text")
        self.assertIn("https://edge.example/relay-img/arch/x.mp4",
                      msgs[0]["text"])
        self.assertIn("my clip", msgs[0]["text"])

    def test_relative_url_resolved_against_lane_base(self):
        relay = self._relay(_FakeHttp(exc=RuntimeError("down")))
        msgs = asyncio.run(relay._media_msgs("/v1/media/x.mp4", "",
                                             "video"))
        self.assertEqual(msgs[-1]["type"], "text")
        self.assertTrue(msgs[-1]["text"].startswith("http"),
                        msgs[-1]["text"])
        self.assertIn("/v1/media/x.mp4", msgs[-1]["text"])

    def test_images_from_still_detects_video_kind(self):
        # detection unchanged — the link policy lives at send time
        triples = line_relay._images_from(
            [{"video_url": "https://x/y.mp4", "title": "t"}])
        self.assertEqual(triples[0][2], "video")


class VideoPosterLink(unittest.TestCase):
    """Happy path: poster image bubble + link text."""

    def _relay(self, http):
        relay = line_relay.LineRelay()
        relay.http = http
        relay._video_poster = lambda data: _POSTER
        return relay

    def _dirs(self, tmp):
        img = os.path.join(tmp, "img-cache")
        arch = os.path.join(tmp, "arch")
        return mock.patch.multiple(
            line_relay, IMG_CACHE_DIR=line_relay.Path(img),
            MEDIA_ARCHIVE_DIR=line_relay.Path(arch),
            LINE_PUBLIC_IMG_BASE="https://pub.example/relay-img")

    def test_public_url_sends_poster_and_original_link(self):
        with tempfile.TemporaryDirectory() as tmp, self._dirs(tmp):
            http = _FakeHttp()
            relay = self._relay(http)
            msgs = asyncio.run(relay._media_msgs(
                "https://pub.example/relay-img/arch/renders/x.mp4",
                "my clip", "video"))
            self.assertEqual([m["type"] for m in msgs],
                             ["image", "text"])
            self.assertTrue(msgs[0]["originalContentUrl"].startswith(
                "https://pub.example/relay-img/vpost-"))
            self.assertIn("https://pub.example/relay-img/arch/renders/"
                          "x.mp4", msgs[1]["text"])
            self.assertIn("my clip", msgs[1]["text"])
            # poster persisted under the nonce lane for post-restart serves
            pnonce = msgs[0]["originalContentUrl"].rsplit("/", 1)[1]
            self.assertTrue(os.path.isfile(
                os.path.join(tmp, "img-cache", pnonce)))
            # already-public source: no arch re-host
            self.assertFalse(os.path.isdir(os.path.join(tmp, "arch")))

    def test_tailnet_url_rehosted_to_public_arch_link(self):
        with tempfile.TemporaryDirectory() as tmp, self._dirs(tmp):
            http = _FakeHttp()
            relay = self._relay(http)
            src = ("https://tony-dell.taila0626a.ts.net/api/"
                   "input-bridge/frame?screen=0&token=render:x")
            msgs = asyncio.run(relay._media_msgs(src, "", "video"))
            self.assertEqual([m["type"] for m in msgs],
                             ["image", "text"])
            self.assertIn("https://pub.example/relay-img/arch/video/",
                          msgs[1]["text"])
            self.assertNotIn("ts.net", msgs[1]["text"])
            # the re-hosted bytes sit under MEDIA_ARCHIVE_DIR/video/
            name = msgs[1]["text"].rsplit("/", 1)[1]
            arch = os.path.join(tmp, "arch", "video", name)
            self.assertTrue(os.path.isfile(arch))
            with open(arch, "rb") as fh:
                self.assertEqual(fh.read(), _MP4)

    def test_poster_failure_still_sends_link(self):
        with tempfile.TemporaryDirectory() as tmp, self._dirs(tmp):
            http = _FakeHttp()
            relay = self._relay(http)
            relay._video_poster = lambda data: None
            msgs = asyncio.run(relay._media_msgs(
                "https://pub.example/x.mp4", "cap", "video"))
            self.assertEqual([m["type"] for m in msgs], ["text"])
            self.assertIn("https://pub.example/x.mp4", msgs[0]["text"])


if __name__ == "__main__":
    unittest.main()
