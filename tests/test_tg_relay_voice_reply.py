"""TG_VOICE_REPLY voice-out bridge (card tg-voice-out-lane).

After a completed Ada turn, an opted-in lane synthesizes the reply with
backend/tts.synth_pcm and POSTs it as audio_b64 to the relay's own /send
listener — the same contract chat_send say= rides. Text still goes out
first; voice is strictly additive. Gate modes: unset/off = never,
'voice' = only replies to voice/audio inbound, '1'/'all' = every
completed turn. Locally gated (trivial-gate) and failed turns stay text.
"""
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend import tg_relay, tts

_PCM = b"\x01\x02" * 2400  # 4800B -> 100ms at 48B/ms (24kHz s16le mono)


def _upd(chat_id=111, sender_id=555, chat_type="private", **msg):
    return {"update_id": 1, "message": {
        "message_id": 9, "date": 1760000000,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": sender_id, "first_name": "Michael",
                 "username": "michaelxyz"},
        **msg}}


def _relay():
    relay = tg_relay.TgRelay()

    async def fake_tg(method, **params):
        if method == "getFile":
            return {"file_path": "voice/file_1.oga"}
        return {}

    relay.tg = fake_tg
    ok_resp = MagicMock()
    ok_resp.json = lambda: {"ok": True, "chat_id": 111}
    get_resp = MagicMock()
    get_resp.content = b"OGGDATA"
    get_resp.raise_for_status = lambda: None
    http = MagicMock()
    http.get = AsyncMock(return_value=get_resp)
    http.post = AsyncMock(return_value=ok_resp)
    relay.http = http
    relay.send_text = AsyncMock()
    return relay


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class VoiceReplyTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patches = [
            patch.object(tg_relay, "VOICE_CORPUS_DIR",
                         Path(self._tmp.name)),
            patch.dict(os.environ, {
                "TELEGRAM_ALLOWED_CHAT_IDS": "111",
                "TG_VOICE_SEND_URL": "http://127.0.0.1:8911/send",
            }),
            patch.object(tg_relay.trivial_gate, "check",
                         AsyncMock(return_value=None)),
            patch.object(tg_relay.ChatSession, "send_turn",
                         AsyncMock(return_value=("sure, lights are on",
                                                 []))),
            patch.object(tts, "synth_pcm", AsyncMock(return_value=_PCM)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("TG_VOICE_REPLY", None)

    def _posted(self, relay):
        return [c for c in relay.http.post.await_args_list
                if str(c.args[0]).endswith("/send")]

    async def test_off_by_default(self):
        relay = _relay()
        await relay.handle(_upd(text="turn on the lights"))
        relay.send_text.assert_awaited_once()  # text reply only
        self.assertEqual(self._posted(relay), [])
        tts.synth_pcm.assert_not_awaited()

    async def test_text_turn_posts_wav_b64(self):
        os.environ["TG_VOICE_REPLY"] = "1"
        relay = _relay()
        await relay.handle(_upd(text="turn on the lights"))
        [call] = self._posted(relay)
        self.assertEqual(call.args[0], "http://127.0.0.1:8911/send")
        body = call.kwargs["json"]
        self.assertEqual(body["chat_id"], 111)
        self.assertEqual(body["audio_mime"], "audio/wav")
        self.assertEqual(body["duration_ms"],
                         tts.pcm_duration_ms(_PCM))
        wav = base64.b64decode(body["audio_b64"])
        self.assertEqual(wav[:4], b"RIFF")
        # Ada's reply (not the tagged text) is what gets spoken.
        tts.synth_pcm.assert_awaited_once()
        self.assertEqual(tts.synth_pcm.await_args.args[0],
                         "sure, lights are on")

    async def test_voice_mode_voices_only_voice_inbound(self):
        os.environ["TG_VOICE_REPLY"] = "voice"
        relay = _relay()
        await relay.handle(_upd(text="turn on the lights"))
        self.assertEqual(self._posted(relay), [])
        with patch.object(tg_relay, "_transcribe",
                          return_value=("turn on the lights", "ok")):
            await relay.handle(_upd(voice={
                "file_id": "F1", "duration": 2,
                "mime_type": "audio/ogg"}))
        self.assertEqual(len(self._posted(relay)), 1)

    async def test_tts_failure_is_text_only(self):
        os.environ["TG_VOICE_REPLY"] = "1"
        tts.synth_pcm.return_value = None
        relay = _relay()
        await relay.handle(_upd(text="turn on the lights"))
        relay.send_text.assert_awaited_once()
        self.assertEqual(self._posted(relay), [])

    async def test_gated_turn_not_voiced(self):
        os.environ["TG_VOICE_REPLY"] = "1"
        tg_relay.trivial_gate.check.return_value = {
            "reply": "You're welcome!"}
        relay = _relay()
        await relay.handle(_upd(text="thanks"))
        self.assertEqual(self._posted(relay), [])
        tts.synth_pcm.assert_not_awaited()

    async def test_failed_turn_not_voiced(self):
        os.environ["TG_VOICE_REPLY"] = "1"
        tg_relay.ChatSession.send_turn.side_effect = RuntimeError("ws")
        relay = _relay()
        await relay.handle(_upd(text="turn on the lights"))
        self.assertEqual(self._posted(relay), [])
        tts.synth_pcm.assert_not_awaited()

    async def test_send_endpoint_audio_b64(self):
        """/send (shared webhook+polling handler) converts WAV audio_b64
        to ogg and pushes a voice bubble — voice replies included."""
        relay = _relay()
        relay.send_voice = AsyncMock()
        with patch.object(relay, "_wav_to_ogg", return_value=b"OGG"):
            resp = await relay._send_endpoint()(_Req({
                "chat_id": 111,
                "audio_b64": base64.b64encode(b"WAVDATA").decode(),
                "duration_ms": 100}))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(json.loads(resp.body)["ok"])
        relay.send_voice.assert_awaited_once_with(111, b"OGG", "", 100)

    async def test_send_endpoint_empty_payload(self):
        relay = _relay()
        resp = await relay._send_endpoint()(_Req({"chat_id": 111}))
        self.assertEqual(resp.status_code, 400)

    def test_send_url_derives_from_listen(self):
        relay = _relay()
        os.environ.pop("TG_VOICE_SEND_URL", None)  # patch.dict restores
        with patch.object(tg_relay, "TG_LISTEN", "127.0.0.1:8913"):
            self.assertEqual(relay._send_url(),
                             "http://127.0.0.1:8913/send")
        with patch.object(tg_relay, "TG_LISTEN", "0.0.0.0:8914"):
            self.assertEqual(relay._send_url(),
                             "http://127.0.0.1:8914/send")


if __name__ == "__main__":
    unittest.main()
