"""TG lanes get the video file itself via sendVideo (hybrid delivery,
card ada-render-video-line-unsendable).

A tool result carrying video_url reaches Telegram as a real file upload
(sendVideo multipart — TG's URL fetch caps at 20MB, uploads take 50MB),
both from the in-turn media path (send_media kind='video') and the
loopback /send contract. On failure it degrades to a text link.
"""
import unittest
from unittest.mock import AsyncMock, MagicMock

from backend import tg_relay

_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4000


def _resp(status=200, content=_MP4, ctype="video/mp4", json_data=None):
    r = MagicMock()
    r.status_code = status
    r.content = content
    r.headers = {"content-type": ctype}
    r.json = lambda: (json_data if json_data is not None else {"ok": True})
    return r


def _relay(get_resp=None, post_resp=None):
    relay = tg_relay.TgRelay()
    http = MagicMock()
    http.get = AsyncMock(
        return_value=get_resp if get_resp is not None else _resp())
    http.post = AsyncMock(
        return_value=post_resp if post_resp is not None else _resp())
    relay.http = http
    relay.send_text = AsyncMock()
    return relay


class SendVideoPath(unittest.IsolatedAsyncioTestCase):

    async def test_send_media_video_uploads_file_via_sendvideo(self):
        relay = _relay()
        await relay.send_media(111, "https://x.example/clip.mp4",
                               "my clip", "video")
        post = relay.http.post.await_args
        self.assertIn("/sendVideo", post.args[0])
        self.assertEqual(post.kwargs["data"]["chat_id"], "111")
        self.assertEqual(post.kwargs["data"]["caption"], "my clip")
        fname, payload, mime = post.kwargs["files"]["video"]
        self.assertEqual(payload, _MP4)
        self.assertTrue(fname.endswith(".mp4"))
        self.assertEqual(mime, "video/mp4")
        relay.send_text.assert_not_awaited()

    async def test_send_media_image_still_sendphoto(self):
        relay = _relay()
        await relay.send_media(111, "https://x.example/x.jpg", "", "image")
        post = relay.http.post.await_args
        self.assertIn("/sendPhoto", post.args[0])

    async def test_video_fetch_failure_falls_back_to_link_text(self):
        relay = _relay(get_resp=_resp(status=404, content=b"x" * 2000))
        await relay.send_video(111, "https://x.example/clip.mp4", "")
        relay.http.post.assert_not_awaited()
        text = relay.send_text.await_args.args[1]
        self.assertIn("https://x.example/clip.mp4", text)

    async def test_oversize_clip_falls_back_to_link_text(self):
        big = _resp(content=b"\x00" * (50 * 1024 * 1024))
        relay = _relay(get_resp=big)
        await relay.send_video(111, "https://x.example/big.mp4", "")
        relay.http.post.assert_not_awaited()
        text = relay.send_text.await_args.args[1]
        self.assertIn("https://x.example/big.mp4", text)

    async def test_images_from_video_url_marks_video_kind(self):
        triples = tg_relay._images_from(
            [{"video_url": "https://x/y.mp4", "title": "t"}])
        self.assertEqual(triples, [("https://x/y.mp4", "t", "video")])


if __name__ == "__main__":
    unittest.main()
