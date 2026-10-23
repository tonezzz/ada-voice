"""Unified voice+chat history (card ada-reads-text-chat):

- every session's turns feed the shared channel log, labelled by surface
- sibling sessions of the same owner can read each other's live turns
- the per-owner+instance persisted tail bridges other ada backends
- mirrored turns land labelled in transcripts but never re-enter the log
"""

import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("ADA_INSTANCE_ID", "test")

from backend import conversation_memory as cm
from backend.conversation_memory import ConversationMemory
from backend.realtime_provider import GeminiLiveProvider


class FakeMddb:
    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.adds: list[dict] = []

    async def add_document(self, collection, key, lang, content_md, meta=None,
                           timeout=None):
        doc = {"key": key, "contentMd": content_md, "meta": meta or {}}
        self.docs[key] = doc
        self.adds.append({"collection": collection, "key": key,
                          "content_md": content_md, "meta": meta})
        return doc

    async def get_document(self, collection, key, lang="en"):
        return self.docs.get(key)

    async def search_documents(self, collection, query="*", filter_meta=None,
                               limit=10):
        out = []
        for d in self.docs.values():
            meta = d.get("meta") or {}
            if filter_meta:
                ok = all(
                    any(v in (meta.get(k) or []) for v in vals)
                    for k, vals in filter_meta.items()
                )
                if not ok:
                    continue
            out.append(d)
        return out[:limit]


def _conv(sid: str, owner: str | None = "person.tony",
          channel: str = "voice") -> ConversationMemory:
    c = ConversationMemory(sid)
    c.channel = channel
    c.owner_identity = owner
    return c


class ChannelLogTests(unittest.TestCase):
    def setUp(self) -> None:
        cm._channel_log.clear()

    def test_turns_enter_shared_log(self) -> None:
        conv = _conv("s1", channel="chat")
        conv.add_user("hi ada")
        conv.add_assistant("hello tony")
        log = cm.channel_log()
        self.assertEqual(len(log), 2)
        self.assertEqual(log[0]["role"], "user")
        self.assertEqual(log[0]["channel"], "chat")
        self.assertEqual(log[0]["owner"], "person.tony")
        self.assertEqual(log[1]["role"], "assistant")

    def test_no_persist_stays_out(self) -> None:
        conv = _conv("s1")
        conv.no_persist = True
        conv.add_user("scenario text")
        self.assertEqual(cm.channel_log(), [])

    def test_tail_labels_channels_and_excludes_session(self) -> None:
        voice = _conv("voice-1")
        chat = _conv("chat-1", channel="chat")
        chat.add_user("texted from the chat card")
        chat.add_assistant("chat reply")
        tail = cm.channel_tail_text(
            owner="person.tony", exclude_session="voice-1")
        self.assertIn("User [chat]: texted from the chat card", tail)
        self.assertIn("Ada [chat]: chat reply", tail)
        # The origin session's own turns are excluded from its prime tail.
        self.assertEqual(
            "", cm.channel_tail_text(
                owner="person.tony", exclude_session="chat-1"))

    def test_tail_is_owner_strict(self) -> None:
        guest = _conv("g1", owner="guest-kk", channel="chat")
        guest.add_user("guest message")
        self.assertEqual(
            "", cm.channel_tail_text(owner="person.tony"))
        self.assertIn("guest message",
                      cm.channel_tail_text(owner="guest-kk"))

    def test_tail_age_filter(self) -> None:
        old = _conv("old-1", channel="chat")
        old.add_user("ancient text")
        self.assertEqual(
            "", cm.channel_tail_text(owner="person.tony", max_age_s=0.0))
        self.assertIn("ancient text",
                      cm.channel_tail_text(owner="person.tony", max_age_s=60))

    def test_tail_turn_cap(self) -> None:
        for i in range(12):
            c = _conv(f"s{i}", channel="telegram")
            c.add_user(f"turn {i}")
        tail = cm.channel_tail_text(owner="person.tony", max_turns=4)
        self.assertEqual(4, tail.count("User [telegram]:"))
        self.assertIn("turn 11", tail)
        self.assertNotIn("turn 0", tail)


class ChannelMirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        cm._channel_log.clear()

    def test_mirror_lands_labelled_not_relogged(self) -> None:
        conv = _conv("v1")
        conv.add_channel_mirror("user", "typed in chat", "chat")
        turns = conv.turns()
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["role"], "user")
        self.assertEqual(turns[0]["text"], "[via chat] typed in chat")
        self.assertTrue(turns[0]["mirrored"])
        # Mirrored turns must not re-enter the shared log (the origin
        # session already logged them) — no echo amplification.
        self.assertEqual(cm.channel_log(), [])

    def test_mirror_in_transcript(self) -> None:
        conv = _conv("v1")
        conv.add_user("spoken aloud")
        conv.add_channel_mirror("user", "typed instead", "chat")
        body = conv.transcript()
        self.assertIn("spoken aloud", body)
        self.assertIn("[via chat] typed instead", body)


class ChannelTailDocTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        cm._channel_log.clear()

    async def test_record_writes_per_instance_doc(self) -> None:
        fake = FakeMddb()
        await cm.record_channel_tail(
            fake, "person.tony", "12:00 User [chat]: hi")
        self.assertEqual(len(fake.adds), 1)
        add = fake.adds[0]
        self.assertEqual(add["collection"], "ada-ha-recall-summary-shared")
        self.assertEqual(add["key"], "channel-tail-person.tony-test")
        self.assertEqual(add["meta"]["kind"], ["channel-tail"])
        self.assertEqual(add["meta"]["owner"], ["person.tony"])
        self.assertEqual(add["meta"]["instance"], ["test"])

    async def test_record_skips_empty(self) -> None:
        fake = FakeMddb()
        await cm.record_channel_tail(fake, None, "x")
        await cm.record_channel_tail(fake, "person.tony", "  ")
        self.assertEqual(fake.adds, [])

    async def test_read_excludes_own_instance_and_foreign_owners(self) -> None:
        fake = FakeMddb()
        await fake.add_document(
            "ada-ha-recall-summary-shared", "channel-tail-person.tony-pwa",
            "en", "pwa tail",
            meta={"kind": ["channel-tail"], "owner": ["person.tony"],
                  "instance": ["pwa"],
                  "updated_ts": [f"{time.time():.3f}"]})
        await fake.add_document(
            "ada-ha-recall-summary-shared", "channel-tail-person.tony-test",
            "en", "own tail",
            meta={"kind": ["channel-tail"], "owner": ["person.tony"],
                  "instance": ["test"],
                  "updated_ts": [f"{time.time():.3f}"]})
        await fake.add_document(
            "ada-ha-recall-summary-shared", "channel-tail-guest-kk-pwa",
            "en", "guest tail",
            meta={"kind": ["channel-tail"], "owner": ["guest-kk"],
                  "instance": ["pwa"],
                  "updated_ts": [f"{time.time():.3f}"]})
        tails = await cm.read_channel_tails(
            fake, "person.tony", exclude_instance="test")
        self.assertEqual(tails, ["pwa tail"])

    async def test_read_orders_by_freshness(self) -> None:
        fake = FakeMddb()
        now = time.time()
        await fake.add_document(
            "ada-ha-recall-summary-shared", "channel-tail-person.tony-a",
            "en", "older tail",
            meta={"kind": ["channel-tail"], "owner": ["person.tony"],
                  "instance": ["a"], "updated_ts": [f"{now - 100:.3f}"]})
        await fake.add_document(
            "ada-ha-recall-summary-shared", "channel-tail-person.tony-b",
            "en", "fresher tail",
            meta={"kind": ["channel-tail"], "owner": ["person.tony"],
                  "instance": ["b"], "updated_ts": [f"{now:.3f}"]})
        tails = await cm.read_channel_tails(
            fake, "person.tony", exclude_instance="test", limit=2)
        self.assertEqual(tails[0], "fresher tail")
        self.assertEqual(tails[1], "older tail")

    async def test_read_drops_stale_docs(self) -> None:
        fake = FakeMddb()
        await fake.add_document(
            "ada-ha-recall-summary-shared", "channel-tail-person.tony-pwa",
            "en", "stale tail",
            meta={"kind": ["channel-tail"], "owner": ["person.tony"],
                  "instance": ["pwa"],
                  "updated_ts": [f"{time.time() - 48 * 3600:.3f}"]})
        self.assertEqual(
            [], await cm.read_channel_tails(
                fake, "person.tony", exclude_instance="test"))


class WebchatHistoryStoreTests(unittest.TestCase):
    """Per-chat JSONL history (card webchat-history-endpoint): web-channel
    turns journal per chat_id and survive restarts; remounts backfill."""

    def setUp(self) -> None:
        self._dir = tempfile.mkdtemp(prefix="wch-")
        self._old_dir = cm._WEBCHAT_HISTORY_DIR
        cm._WEBCHAT_HISTORY_DIR = self._dir
        cm._history_counts.clear()

    def tearDown(self) -> None:
        cm._WEBCHAT_HISTORY_DIR = self._old_dir
        cm._history_counts.clear()
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_append_and_read_ordered(self) -> None:
        cm.webchat_history_append("web-kk", "user", "hi ada", ts=1.5)
        cm.webchat_history_append("web-kk", "assistant", "hello kk")
        turns = cm.webchat_history("web-kk")
        self.assertEqual([t["role"] for t in turns], ["user", "assistant"])
        self.assertEqual(turns[0]["text"], "hi ada")
        self.assertEqual(turns[0]["ts"], 1.5)
        self.assertNotIn("image_url", turns[0])

    def test_threads_are_per_chat_id(self) -> None:
        cm.webchat_history_append("web-kk", "user", "kk text")
        cm.webchat_history_append("web-tony", "user", "tony text")
        self.assertEqual([t["text"] for t in cm.webchat_history("web-kk")],
                         ["kk text"])
        self.assertEqual([t["text"] for t in cm.webchat_history("web-tony")],
                         ["tony text"])

    def test_max_turns_takes_tail(self) -> None:
        for i in range(6):
            cm.webchat_history_append("web-kk", "user", f"turn {i}")
        turns = cm.webchat_history("web-kk", max_turns=2)
        self.assertEqual([t["text"] for t in turns],
                         ["turn 4", "turn 5"])

    def test_media_only_push_kept_empty_dropped(self) -> None:
        cm.webchat_history_append(
            "web-kk", "assistant", "", image_url="http://x/i.jpg")
        cm.webchat_history_append("web-kk", "assistant", "   ")
        cm.webchat_history_append("", "user", "no chat")
        turns = cm.webchat_history("web-kk")
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["image_url"], "http://x/i.jpg")

    def test_bad_lines_skipped(self) -> None:
        path = cm._webchat_history_path("web-kk")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'not json\n{"role":"user","text":"ok","ts":1}\n{"x":1}\n',
            encoding="utf-8")
        turns = cm.webchat_history("web-kk")
        self.assertEqual([t["text"] for t in turns], ["ok"])

    def test_rolls_over_at_keep(self) -> None:
        old_keep, old_trim = cm._WEBCHAT_HISTORY_KEEP, cm._WEBCHAT_HISTORY_TRIM_AT
        cm._WEBCHAT_HISTORY_KEEP, cm._WEBCHAT_HISTORY_TRIM_AT = 5, 6
        try:
            for i in range(9):
                cm.webchat_history_append("web-kk", "user", f"turn {i}")
        finally:
            cm._WEBCHAT_HISTORY_KEEP, cm._WEBCHAT_HISTORY_TRIM_AT = (
                old_keep, old_trim)
        turns = cm.webchat_history("web-kk")
        self.assertEqual(len(turns), 5)
        self.assertEqual(turns[0]["text"], "turn 4")
        # Post-restart path: counts re-sync from the trimmed file, so the
        # next append lands at KEEP+1 instead of re-trimming at the stale
        # in-memory high-water mark.
        cm._history_counts.clear()
        cm.webchat_history_append("web-kk", "user", "turn 9")
        turns = cm.webchat_history("web-kk")
        self.assertEqual([t["text"] for t in turns],
                         [f"turn {i}" for i in range(4, 10)])


class WebchatJournalTests(unittest.TestCase):
    """_channel_append routes web-channel turns into the per-chat log."""

    def setUp(self) -> None:
        cm._channel_log.clear()
        self._dir = tempfile.mkdtemp(prefix="wch-")
        self._old_dir = cm._WEBCHAT_HISTORY_DIR
        cm._WEBCHAT_HISTORY_DIR = self._dir
        cm._history_counts.clear()

    def tearDown(self) -> None:
        cm._WEBCHAT_HISTORY_DIR = self._old_dir
        cm._history_counts.clear()
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_web_turns_journal_per_chat(self) -> None:
        conv = _conv("s1", channel="web")
        conv.chat_id = "web-kk"
        conv.add_user("hi ada")
        conv.add_assistant("hello kk")
        turns = cm.webchat_history("web-kk")
        self.assertEqual([t["role"] for t in turns], ["user", "assistant"])

    def test_voice_and_unbound_never_journal(self) -> None:
        voice = _conv("v1", channel="voice")
        voice.add_user("spoken")
        unbound = _conv("w1", channel="web")
        unbound.add_user("no chat_id bound")
        self.assertEqual(cm.webchat_history("web-kk"), [])
        self.assertFalse(
            os.listdir(cm._WEBCHAT_HISTORY_DIR))

    def test_no_persist_stays_out(self) -> None:
        conv = _conv("s1", channel="web")
        conv.chat_id = "web-kk"
        conv.no_persist = True
        conv.add_user("scenario text")
        self.assertEqual(cm.webchat_history("web-kk"), [])

    def test_mirrored_turns_do_not_relog(self) -> None:
        # A sibling's turn mirrored into a web session must not land in
        # the thread a second time — the origin session already wrote it.
        conv = _conv("s1", channel="web")
        conv.chat_id = "web-kk"
        conv.add_channel_mirror("user", "typed elsewhere", "chat")
        self.assertEqual(cm.webchat_history("web-kk"), [])


class ProviderNoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_context_note_defers_silently(self) -> None:
        provider = GeminiLiveProvider(session_id="t1")
        sent: list[str] = []

        async def fake_send(text: str) -> None:
            sent.append(text)

        provider._send_context_note = fake_send  # type: ignore[assignment]
        provider.queue_context_note("(system) chat channel: user wrote 'hi'")
        self.assertEqual(len(provider._pending_notifications), 1)
        # Draining delivers the note through the silent context path.
        await provider._drain_notifications()
        self.assertEqual(sent, ["(system) chat channel: user wrote 'hi'"])


class PrimeTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_context_included(self) -> None:
        from backend import memory_ops

        fake = FakeMddb()
        registry = MagicMock()
        registry.bank.side_effect = KeyError("no bank")
        text = await memory_ops.session_prime_text(
            fake, registry, summary=None,
            channel_context="12:00 User [chat]: texted earlier",
        )
        self.assertIsNotNone(text)
        self.assertIn("other chat channels", text)
        self.assertIn("User [chat]: texted earlier", text)


if __name__ == "__main__":
    unittest.main()
