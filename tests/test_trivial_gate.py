"""trivial_gate (card ada-nest-trivial-gate): the adaptive-depth gate on
the TG relay cascade — anchored L0 rules, optional L1 /v1/systemone
intent classifier, hot-session suppression for answer-shaped acks,
corpus rows + deflection-rate accounting, and the relay wiring that
skips the Ada turn on a deflect."""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GEMINI_API_KEY", "x")

from backend import tg_relay, trivial_gate  # noqa: E402

_CORPUS = tempfile.NamedTemporaryFile(
    delete=False, suffix=".jsonl").name

_BASE_ENV = {
    "ADA_TRIVIAL_GATE": "enforce",
    "ADA_TRIVIAL_LLM_URL": "",
    "ADA_COMMAND_LLM_URL": "",
    "ADA_JEV_URL": "",
    "ADA_TRIVIAL_CORPUS": _CORPUS,
    "ADA_TRIVIAL_MIN_CONF": "0.75",
    "ADA_TRIVIAL_QUIET_S": "90",
    "TG_PERSONA_NAME": "Ada",
    "TG_REPLY_TAG": "",
    "TG_SPEAKER_TAG": "",
}


def _rows():
    f = Path(_CORPUS)
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]


def _resp(answer: dict):
    body = json.dumps({"answers": {"q": answer}}).encode()
    m = MagicMock()
    m.read.return_value = body
    return m


class _EnvTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Path(_CORPUS).write_text("")
        trivial_gate.reset()
        self._p = patch.dict(os.environ, _BASE_ENV)
        self._p.start()
        self.addCleanup(self._p.stop)


class RuleIntentTests(_EnvTest):

    async def _eval(self, text, **kw):
        kw.setdefault("surface", "text")
        return await trivial_gate.evaluate(text, persona="Ada", **kw)

    async def test_greetings(self):
        for t in ("hi", "Hi Ada!", "hello there", "good morning",
                  "hey everyone", "สวัสดี", "morning"):
            row = await self._eval(t)
            self.assertEqual(row["intent"], "greeting", t)
            self.assertEqual(row["action"], "deflect", t)
            self.assertEqual(row["layer"], "rules")

    async def test_greeting_reply_mirrors_daypart(self):
        row = await self._eval("good morning ada")
        self.assertEqual(row["reply"], "Good morning!")
        row = await self._eval("hi ada")
        self.assertEqual(row["reply"], "Hey!")

    async def test_thanks_bye_kudos(self):
        for t, intent in (("thank you", "thanks"),
                          ("thanks ada", "thanks"),
                          ("ขอบคุณ", "thanks"),
                          ("good night", "bye"),
                          ("see you later", "bye"),
                          ("got it", "kudos"),
                          ("awesome", "kudos")):
            row = await self._eval(t)
            self.assertEqual(row["intent"], intent, t)
            self.assertEqual(row["action"], "deflect", t)

    async def test_time_date(self):
        row = await self._eval("what time is it")
        self.assertEqual(row["intent"], "time")
        self.assertIn("M", row["reply"])  # "10:31 AM"
        row = await self._eval("what day is today")
        self.assertEqual(row["intent"], "time")
        self.assertIn("2026", row["reply"])  # date answer
        row = await self._eval("กี่โมง")
        self.assertEqual(row["intent"], "time")

    async def test_identity_and_chat(self):
        row = await self._eval("who are you")
        self.assertEqual(row["intent"], "identity")
        self.assertIn("Ada", row["reply"])
        row = await self._eval("how are you")
        self.assertEqual(row["intent"], "chat")

    async def test_emoji_only(self):
        row = await self._eval("❤️")
        self.assertEqual(row["intent"], "emoji")
        self.assertEqual(row["reply"], "👍")

    async def test_real_queries_pass(self):
        for t in ("turn on the bedroom fan",
                  "what time is it in bangkok",
                  "thanks can you open the gate",
                  "morning briefing please",
                  "yes it worked thanks",
                  "what's the weather like"):
            row = await self._eval(t)
            self.assertEqual(row["action"], "pass", t)
            self.assertIsNone(row["intent"], t)

    async def test_answerish_suppressed_when_hot(self):
        for t in ("yes", "ok", "sure", "👍"):
            row = await self._eval(t, hot=True)
            self.assertEqual(row["action"], "pass", t)
            self.assertEqual(row["reason"], "hot-session", t)
            row = await self._eval(t, hot=False)
            self.assertEqual(row["action"], "deflect", t)

    async def test_pleasantries_not_suppressed_when_hot(self):
        for t in ("thanks", "good night", "hi ada"):
            row = await self._eval(t, hot=True)
            self.assertEqual(row["action"], "deflect", t)


class ModeTests(_EnvTest):

    async def test_off_mode_skips_everything(self):
        with patch.dict(os.environ, {"ADA_TRIVIAL_GATE": "off"}):
            out = await trivial_gate.check(
                "hi ada", surface="text", persona="Ada")
            self.assertIsNone(out)
            await asyncio.sleep(0.05)
            self.assertEqual(_rows(), [])
            self.assertEqual(trivial_gate.stats()["evaluated"], 0)

    async def test_shadow_records_but_never_answers(self):
        with patch.dict(os.environ, {"ADA_TRIVIAL_GATE": "shadow"}):
            out = await trivial_gate.check(
                "thank you", surface="text", persona="Ada")
            self.assertIsNone(out)
            await asyncio.sleep(0.05)
            [row] = _rows()
            self.assertEqual(row["action"], "would-deflect")
            self.assertEqual(row["intent"], "thanks")
            self.assertEqual(
                trivial_gate.stats()["would_deflect"], 1)

    async def test_enforce_returns_reply(self):
        out = await trivial_gate.check(
            "good night", surface="text", persona="Ada")
        self.assertIsNotNone(out)
        self.assertEqual(out["reply"], "Good night!")
        out = await trivial_gate.check(
            "tell me a story", surface="text", persona="Ada")
        self.assertIsNone(out)


class L1Tests(_EnvTest):

    def _env(self):
        return patch.dict(os.environ, {
            "ADA_TRIVIAL_LLM_URL": "http://shelf.test"})

    async def test_model_deflects_on_confident_intent(self):
        with self._env(), patch.object(
                trivial_gate.urllib.request, "urlopen",
                lambda req, timeout=None: _resp(
                    {"choice": "greeting", "confidence": 0.9})):
            row = await trivial_gate.evaluate(
                "well hello there friend", surface="text", persona="Ada")
        self.assertEqual(row["action"], "deflect")
        self.assertEqual(row["layer"], "model")
        self.assertEqual(row["intent"], "greeting")

    async def test_low_confidence_passes(self):
        with self._env(), patch.object(
                trivial_gate.urllib.request, "urlopen",
                lambda req, timeout=None: _resp(
                    {"choice": "greeting", "confidence": 0.4})):
            row = await trivial_gate.evaluate(
                "well hello there friend", surface="text", persona="Ada")
        self.assertEqual(row["action"], "pass")
        self.assertIn("l1=", row["reason"])

    async def test_pass_choice_and_long_text(self):
        calls = []

        def fake(req, timeout=None):
            calls.append(req)
            return _resp({"choice": "pass", "confidence": 0.99})

        with self._env(), patch.object(
                trivial_gate.urllib.request, "urlopen", fake):
            row = await trivial_gate.evaluate(
                "something unclassifiable", surface="text",
                persona="Ada")
            self.assertEqual(row["action"], "pass")
            # over the word cap the model hop never fires
            row = await trivial_gate.evaluate(
                "word " * 25, surface="text", persona="Ada")
            self.assertEqual(row["action"], "pass")
        self.assertEqual(len(calls), 1)

    async def test_l1_failure_falls_through(self):
        def boom(req, timeout=None):
            raise OSError("shelf down")

        with self._env(), patch.object(
                trivial_gate.urllib.request, "urlopen", boom):
            row = await trivial_gate.evaluate(
                "well hello there friend", surface="text",
                persona="Ada")
        self.assertEqual(row["action"], "pass")


def _relay(tmpdir):
    relay = tg_relay.TgRelay()
    sent = []

    async def fake_tg(method, **params):
        if method == "getFile":
            return {"file_path": "voice/file_1.oga"}
        sent.append((method, params))
        return {}

    relay.tg = fake_tg
    relay._sent = sent
    resp = MagicMock()
    resp.content = b"OGGDATA"
    resp.raise_for_status = lambda: None
    http = MagicMock()
    http.get = AsyncMock(return_value=resp)
    relay.http = http
    return relay


def _upd(chat_id=111, sender_id=555, chat_type="private", **msg):
    return {"update_id": 1, "message": {
        "message_id": 9, "date": 1760000000,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": sender_id, "first_name": "Tony",
                 "username": "tonyxyz"},
        **msg}}


class RelayWiringTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        Path(_CORPUS).write_text("")
        trivial_gate.reset()
        self._p = patch.dict(os.environ, {
            **_BASE_ENV,
            "TELEGRAM_ALLOWED_CHAT_IDS": "111,-222",
            "TELEGRAM_USER_CALLERS": "555:user-tony",
            "TELEGRAM_CHAT_CALLERS": "",
        })
        self._p.start()
        self.addCleanup(self._p.stop)
        self._vp = patch.object(
            tg_relay, "VOICE_CORPUS_DIR", Path(self._tmp.name))
        self._vp.start()
        self.addCleanup(self._vp.stop)

    def _texts(self, relay):
        return [p["text"] for m, p in relay._sent if m == "sendMessage"]

    async def test_text_trivial_deflected_no_session(self):
        relay = _relay(self._tmp.name)
        await relay.handle(_upd(text="thank you"))
        self.assertEqual(relay.sessions, {})
        self.assertIn("You're welcome!",
                      self._texts(relay)[-1])

    async def test_text_real_query_reaches_ada(self):
        relay = _relay(self._tmp.name)
        sess = MagicMock()
        sess.ws = None
        sess.last_active = 0.0
        sess.send_turn = AsyncMock(return_value=("fan is on", []))
        sess._current = asyncio.Queue()
        relay.sessions[111] = sess
        await relay.handle(_upd(text="is the bedroom fan on"))
        sess.send_turn.assert_awaited_once_with(
            "is the bedroom fan on")
        self.assertIn("fan is on", self._texts(relay)[-1])

    async def test_text_ack_hot_session_passes_through(self):
        """'yes' answering Ada's question must reach Ada — the hot-
        session suppression keeps answer-shaped acks off the gate."""
        relay = _relay(self._tmp.name)
        sess = MagicMock()
        sess.ws = object()
        sess.last_active = time.monotonic()
        sess.send_turn = AsyncMock(return_value=("done!", []))
        sess._current = asyncio.Queue()
        relay.sessions[111] = sess
        await relay.handle(_upd(text="yes"))
        sess.send_turn.assert_awaited_once_with("yes")
        self.assertIn("done!", self._texts(relay)[-1])

    async def test_text_ack_cold_deflected(self):
        relay = _relay(self._tmp.name)
        await relay.handle(_upd(text="ok"))
        self.assertEqual(relay.sessions, {})
        self.assertIn("👍", self._texts(relay)[-1])

    async def test_forwarded_text_bypasses_gate(self):
        relay = _relay(self._tmp.name)
        sess = MagicMock()
        sess.ws = None
        sess.last_active = 0.0
        sess.send_turn = AsyncMock(return_value=("noted", []))
        sess._current = asyncio.Queue()
        relay.sessions[111] = sess
        await relay.handle(_upd(
            text="thank you",
            forward_origin={"type": "hidden_user",
                            "sender_user_name": "Someone"}))
        sess.send_turn.assert_awaited_once()
        self.assertIn("forwarded from",
                      sess.send_turn.await_args.args[0])

    async def test_voice_transcript_deflected(self):
        relay = _relay(self._tmp.name)
        with patch.object(tg_relay, "_transcribe",
                          lambda p: ("hello ada", "ok")):
            await relay.handle(_upd(voice={
                "file_id": "F1", "duration": 2,
                "mime_type": "audio/ogg"}))
        self.assertEqual(relay.sessions, {})
        texts = self._texts(relay)
        self.assertIn('"hello ada"', texts[-2])  # transcript echo kept
        self.assertEqual(texts[-1], "Hey!")

    async def test_voice_real_transcript_reaches_ada(self):
        relay = _relay(self._tmp.name)
        sess = MagicMock()
        sess.ws = None
        sess.last_active = 0.0
        sess.send_turn = AsyncMock(return_value=("lamp off", []))
        sess._current = asyncio.Queue()
        relay.sessions[111] = sess
        with patch.object(tg_relay, "_transcribe",
                          lambda p: ("turn off the desk lamp", "ok")):
            await relay.handle(_upd(voice={
                "file_id": "F2", "duration": 3,
                "mime_type": "audio/ogg"}))
        sess.send_turn.assert_awaited_once()
        self.assertIn("turn off the desk lamp",
                      sess.send_turn.await_args.args[0])
        self.assertIn("lamp off", self._texts(relay)[-1])

    async def test_shadow_mode_relays_everything_but_records(self):
        with patch.dict(os.environ, {"ADA_TRIVIAL_GATE": "shadow"}):
            relay = _relay(self._tmp.name)
            sess = MagicMock()
            sess.ws = None
            sess.last_active = 0.0
            sess.send_turn = AsyncMock(return_value=("hi there!", []))
            sess._current = asyncio.Queue()
            relay.sessions[111] = sess
            await relay.handle(_upd(text="hi ada"))
            sess.send_turn.assert_awaited_once_with("hi ada")
            await asyncio.sleep(0.05)
            [row] = _rows()
            self.assertEqual(row["action"], "would-deflect")
            self.assertEqual(row["surface"], "text")


if __name__ == "__main__":
    unittest.main()
