"""Integration test — recorded voice request -> kanban card spec ->
queued card JSON on the board (card
ada-tool-voice-request-well-formed-kanba-56ba9f).

The "recorded voice input" is tests/fixtures/voice-kanban-request.txt —
the verbatim transcript the voice-to-text pipeline hands the model. The
test drives the real kanban tool end-to-end against the scenario
engine's FakeBoard (the same board-api contract board_client speaks):
draft validates the transcript into a card spec with no board write,
then file lands it on the board with queue=true under Tony's confirm —
the done-condition is the resulting card JSON sitting in the board
queue (column=backlog, action.status=queued, spec/verify/envelope all
present and schema-valid).
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend import board_client as board_client_mod
from tests.scenario_engine import FakeBoard

ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = ROOT / "backend" / "tools.d" / "kanban.py"
TRANSCRIPT = (ROOT / "tests" / "fixtures"
              / "voice-kanban-request.txt")


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "tools_d.kanban_voice_test", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


board = _load_module()


def _runner():
    """Owner-session ToolRunner stand-in; the confirm gate emulates the
    real one (confirmed=True grants, anything else denies)."""
    r = MagicMock()
    r.policy_identity.return_value = "person.tony"
    r.banks.policy_for.return_value = {"full": True}
    r._is_secondary_turn.return_value = False

    def gate(name, args, confirmed, token, instruction):
        if confirmed is True or token == "good-token":
            return None
        raise PermissionError(
            f"{instruction} TO EXECUTE: replay with confirmed=true.")

    r._require_confirmation.side_effect = gate
    return r


class VoiceRequestToCardTest(unittest.IsolatedAsyncioTestCase):

    async def test_recorded_voice_request_files_queued_card(self):
        transcript = TRANSCRIPT.read_text(encoding="utf-8").strip()
        self.assertIn("ada-voice", transcript)
        fake = FakeBoard({})
        runner = _runner()
        with patch.object(board_client_mod, "request", fake.handle):
            # 1. The recorded transcript -> validated card spec.
            draft = await board.run(runner, action="draft",
                                    request=transcript)
            self.assertTrue(draft["ok"], draft)
            d = draft["draft"]
            self.assertEqual(d["column"], "backlog")
            self.assertTrue(d["spec"].startswith("Goal: "))
            self.assertTrue(d["verify"])
            self.assertEqual(d["action"],
                             {"type": "dispatch", "repo": "ada-voice"})
            # draft is read-only — nothing hit the board yet.
            self.assertFalse(fake.calls)

            # 2. Tony confirms the draft aloud -> file with queue=true.
            filed = await board.run(
                runner, action="file",
                title=d["title"], spec=d["spec"], verify=d["verify"],
                tags=d["tags"], priority=d["priority"],
                column=d["column"], card_action=d.get("action"),
                queue=True, confirmed=True)
            self.assertTrue(filed["ok"], filed)
            cid = filed["id"]

        # 3. Done-condition: correctly formatted card JSON in the queue.
        card = fake._find(cid)
        self.assertIsNotNone(card)
        self.assertEqual(card["column"], "backlog")
        self.assertEqual(card["from"], "ada")
        self.assertEqual(card["action"]["type"], "dispatch")
        self.assertEqual(card["action"]["repo"], "ada-voice")
        self.assertEqual(card["action"]["status"], "queued")
        self.assertIn(transcript.split(".")[0], card["spec"])
        self.assertTrue(card["verify"])
        self.assertIn("voice-draft", card["tags"])

        # The POST /card body itself was schema-shaped: only board-card
        # fields, action keys all in the board ACTION_KEYS mirror.
        post = next(c for c in fake.calls
                    if c[0] == "POST" and c[1] == "/card")
        body = post[2]
        self.assertLessEqual(
            set(body),
            {"id", "title", "ref", "column", "priority", "tags", "spec",
             "note", "brief", "verify", "metric", "report", "text",
             "action", "queue", "on_exists", "from"})
        # FakeBoard records calls by shallow-copying the body, so the
        # board-added action.status leaks into the recorded envelope —
        # allow it; the caller-set keys must still be schema keys.
        self.assertLessEqual(
            set(body["action"]), board._CARD_ACTION_KEYS | {"status"})

    async def test_queue_without_confirm_files_nothing(self):
        transcript = TRANSCRIPT.read_text(encoding="utf-8").strip()
        fake = FakeBoard({})
        runner = _runner()
        with patch.object(board_client_mod, "request", fake.handle):
            out = await board.run(runner, action="file",
                                  title="unconfirmed", queue=True)
        self.assertFalse(out["ok"])
        self.assertTrue(out.get("needs_confirm"))
        self.assertFalse(fake.cards)


if __name__ == "__main__":
    unittest.main()
