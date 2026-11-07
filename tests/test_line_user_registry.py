"""First-seen room member registry (card line-group-member-registry).

LINE's members API is gated to verified accounts (404 on
/v2/bot/group/<id>/members/ids since 2026-10-11), so a new room's
members can't be pre-enumerated — the relay learns them as they first
speak and upserts mddb line-user-registry/<userId> with userId,
groupId(s), displayName when LINE shares it, and first/last seen. A
first-seer is status 'pending' until the operator binds the userId in
LINE_USER_CALLERS; the next sighting then flips the doc to 'bound'.
"""
import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend import line_relay


def _ev(text="ada, hi", user_id="Unhong1", group_id="G1", mtype="text",
        etype="message", src_type=None):
    if group_id:
        key = "roomId" if src_type == "room" else "groupId"
        src = {"type": src_type or "group", key: group_id,
               "userId": user_id}
    else:
        src = {"type": "user", "userId": user_id}
    return {"type": etype, "replyToken": "RT", "source": src,
            "message": {"id": "m1", "type": mtype, "text": text}}


class _FakeSession:
    """Stands in for ChatSession — never dials out."""

    def __init__(self, user_id, relay, ws_url=None, caller=None,
                 chat_id=""):
        self.user_id = user_id
        self.ws_url = ws_url
        self.caller = caller
        self.chat_id = chat_id
        self.ws = None
        self.last_active = 0.0
        self.send_turn = AsyncMock(return_value=("pong", []))
        self.send_image_turn = AsyncMock(return_value=("pic pong", []))


class _FakeMddb:
    """In-memory stand-in for MddbClient — records get/add calls."""

    def __init__(self, docs=None):
        self.docs = dict(docs or {})
        self.gets = []
        self.adds = []

    async def get_document(self, collection, key, lang="en",
                           prefer_leader=False):
        self.gets.append((collection, key))
        return self.docs.get((collection, key))

    async def add_document(self, collection, key, lang, content_md,
                           meta=None, **kw):
        self.adds.append((collection, key, content_md, meta))
        self.docs[(collection, key)] = {"contentMd": content_md,
                                        "meta": meta or {}}
        return {"ok": True}


async def _drain():
    """Let fire-and-forget registry tasks finish."""
    tasks = [t for t in asyncio.all_tasks()
             if t is not asyncio.current_task() and not t.done()]
    if tasks:
        await asyncio.gather(*tasks)


def _relay():
    relay = line_relay.LineRelay()
    relay.send_text = AsyncMock()
    relay._deliver = AsyncMock()
    relay.line = AsyncMock(return_value=MagicMock(status_code=200))
    http = MagicMock()
    http.get = AsyncMock(
        return_value=MagicMock(status_code=404, content=b"x"))
    relay.http = http
    return relay


class MemberRegistryTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.mddb = _FakeMddb()
        self._patches = [
            patch.object(line_relay, "_registry_store",
                         lambda: self.mddb),
            patch.object(line_relay, "ChatSession", _FakeSession),
            patch.object(line_relay, "_ops_event", MagicMock()),
            patch.dict(os.environ, {
                "LINE_ALLOWED_USER_IDS": "Utony1",
                "LINE_ALLOWED_GROUP_IDS": "G1",
                "LINE_USER_CALLERS": "Utony1:user-tony",
                "LINE_NAME_BACKENDS": "ada:ws://127.0.0.1:8002",
                "LINE_DEFAULT_PERSONA": "",
            }),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _meta(self):
        [(coll, key, body, meta)] = self.mddb.adds
        self.assertEqual(coll, "line-user-registry")
        return key, body, meta

    async def test_unmapped_first_message_registers_pending(self):
        relay = _relay()
        await relay.handle_event(_ev("ada, hi", user_id="Unhong1"))
        await _drain()
        key, _body, meta = self._meta()
        self.assertEqual(key, "Unhong1")
        self.assertEqual(meta["user_id"], ["Unhong1"])
        self.assertEqual(meta["group_ids"], ["G1"])
        self.assertEqual(meta["status"], ["pending"])
        self.assertNotIn("caller", meta)
        self.assertEqual(len(meta["first_seen"]), 1)
        self.assertEqual(len(meta["last_seen"]), 1)

    async def test_unaddressed_chatter_also_registers(self):
        """First-seen happens on ANY message in the room — an unmapped
        member's unaddressed chatter is ignored for routing but still
        registers them for binding."""
        relay = _relay()
        await relay.handle_event(_ev("random chatter",
                                     user_id="Unhong1"))
        await _drain()
        key, _body, meta = self._meta()
        self.assertEqual(key, "Unhong1")
        self.assertEqual(meta["status"], ["pending"])

    async def test_display_name_recorded_when_line_shares_it(self):
        relay = _relay()
        prof = AsyncMock(return_value=MagicMock(status_code=200))
        prof.return_value.json.return_value = {"displayName": "Nhong"}
        relay.http.get = prof
        await relay.handle_event(_ev(user_id="Unhong1"))
        await _drain()
        _key, _body, meta = self._meta()
        self.assertEqual(meta["display_name"], ["Nhong"])
        # the profile name also seeds the label cache
        self.assertEqual(relay._names["Unhong1"], "Nhong")
        url = prof.await_args.args[0]
        self.assertIn("/v2/bot/group/G1/member/Unhong1", url)

    async def test_no_name_when_profile_gated(self):
        """An unverified OA 404s the member profile — the doc records
        the ids anyway, no display_name."""
        relay = _relay()
        await relay.handle_event(_ev(user_id="Unhong1"))
        await _drain()
        _key, body, meta = self._meta()
        self.assertNotIn("display_name", meta)
        self.assertIn("Unhong1", body)
        self.assertIn("LINE_USER_CALLERS", body)

    async def test_mapped_member_registers_bound(self):
        relay = _relay()
        await relay.handle_event(_ev(user_id="Utony1"))
        await _drain()
        key, _body, meta = self._meta()
        self.assertEqual(key, "Utony1")
        self.assertEqual(meta["status"], ["bound"])
        self.assertEqual(meta["caller"], ["user-tony"])

    async def test_binding_flips_pending_to_bound(self):
        """Operator binds the userId in LINE_USER_CALLERS — the member's
        next message merges group_ids, keeps the original first_seen,
        and flips status to bound."""
        self.mddb.docs[("line-user-registry", "Unhong1")] = {
            "contentMd": "operator note",
            "meta": {"user_id": ["Unhong1"], "group_ids": ["G0"],
                     "first_seen": ["2026-10-20T00:00:00"],
                     "last_seen": ["2026-10-20T00:00:00"],
                     "status": ["pending"]}}
        relay = _relay()
        with patch.dict(os.environ,
                        {"LINE_USER_CALLERS":
                         "Utony1:user-tony,Unhong1:user-nhong"}):
            await relay.handle_event(_ev(user_id="Unhong1"))
            await _drain()
        key, body, meta = self._meta()
        self.assertEqual(meta["group_ids"], ["G0", "G1"])
        self.assertEqual(meta["first_seen"],
                         ["2026-10-20T00:00:00"])
        self.assertEqual(meta["status"], ["bound"])
        self.assertEqual(meta["caller"], ["user-nhong"])
        self.assertEqual(body, "operator note")  # body preserved

    async def test_dedupes_to_one_check_per_process(self):
        relay = _relay()
        for _ in range(3):
            await relay.handle_event(_ev(user_id="Unhong1"))
        await _drain()
        self.assertEqual(len(self.mddb.gets), 1)
        self.assertEqual(len(self.mddb.adds), 1)

    async def test_failed_write_rearms_after_cooldown(self):
        self.mddb.add_document = AsyncMock(return_value=None)
        relay = _relay()
        with patch.object(line_relay, "LINE_REGISTRY_RETRY_S", 0.01):
            await relay.handle_event(_ev(user_id="Unhong1"))
            await _drain()
            self.assertEqual(self.mddb.add_document.await_count, 1)
            # inside the cooldown window a second event doesn't retry
            relay._reg_try["Unhong1"] = 1e30
            await relay.handle_event(_ev("ada, again",
                                         user_id="Unhong1"))
            await _drain()
            self.assertEqual(self.mddb.add_document.await_count, 1)
            # cooldown elapsed -> next event retries
            relay._reg_try["Unhong1"] = 0.0
            await relay.handle_event(_ev("ada, again",
                                         user_id="Unhong1"))
            await _drain()
            self.assertEqual(self.mddb.add_document.await_count, 2)
        self.assertNotIn("Unhong1", relay._reg_seen)

    async def test_disabled_env_skips_registry(self):
        relay = _relay()
        with patch.object(line_relay, "_registry_store",
                          lambda: None):
            await relay.handle_event(_ev(user_id="Unhong1"))
            await _drain()
        self.assertEqual(self.mddb.gets, [])
        self.assertEqual(self.mddb.adds, [])

    async def test_unallowed_group_does_not_register(self):
        relay = _relay()
        await relay.handle_event(_ev(group_id="Gbad",
                                     user_id="Unhong1"))
        await _drain()
        self.assertEqual(self.mddb.gets, [])

    async def test_dm_sender_does_not_register(self):
        relay = _relay()
        await relay.handle_event(_ev(group_id=None, user_id="Ustray"))
        await _drain()
        self.assertEqual(self.mddb.gets, [])

    async def test_registry_failure_never_breaks_turn(self):
        """A registry that raises must not stall or break the routed
        turn — the refusal/turn path proceeds regardless."""
        self.mddb.get_document = AsyncMock(
            side_effect=RuntimeError("mddb down"))
        relay = _relay()
        await relay.handle_event(_ev("ada, hi", user_id="Unhong1"))
        await _drain()
        [call] = [c for c in relay.send_text.await_args_list
                  if "Not authorized in this room" in c.args[1]]
        self.assertIn("Unhong1", call.args[1])


if __name__ == "__main__":
    unittest.main()
