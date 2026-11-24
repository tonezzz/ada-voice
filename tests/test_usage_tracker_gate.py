"""Usage-ledger persistence gate (card live-voice-burn-context-replay).

With ADA_USAGE_LOG unset, the implicit default (data/usage.jsonl) must
persist only for lane-identified processes. Tests executed outside
pytest's conftest — `python tests/test_x.py`, `python -m unittest` —
and ad-hoc shells carry no ADA_INSTANCE_ID; before the gate their rows
were the t-ctx pollution (~4.4M fake in-tokens in the shared ledger).
Explicit ADA_USAGE_LOG= values always win.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from backend.usage_tracker import UsageLedger


class LaneGateTests(unittest.TestCase):
    def test_no_lane_no_default_persistence(self):
        with patch.dict(os.environ):
            os.environ.pop("ADA_USAGE_LOG", None)
            os.environ.pop("ADA_INSTANCE_ID", None)
            ledger = UsageLedger()
            self.assertIsNone(ledger._path)
            with tempfile.TemporaryDirectory() as td:
                cwd = os.getcwd()
                os.chdir(td)
                try:
                    ledger.record("live", session_id="t-x",
                                  input_tokens=100)
                    self.assertFalse(os.path.exists(
                        os.path.join(td, "data", "usage.jsonl")))
                finally:
                    os.chdir(cwd)

    def test_lane_set_keeps_default_path(self):
        with patch.dict(os.environ):
            os.environ.pop("ADA_USAGE_LOG", None)
            os.environ["ADA_INSTANCE_ID"] = "tony"
            self.assertEqual(UsageLedger()._path, "data/usage.jsonl")

    def test_explicit_values_win_over_gate(self):
        with patch.dict(os.environ):
            os.environ.pop("ADA_INSTANCE_ID", None)
            os.environ["ADA_USAGE_LOG"] = "off"
            self.assertIsNone(UsageLedger()._path)
            os.environ["ADA_USAGE_LOG"] = ""
            self.assertIsNone(UsageLedger()._path)
            with tempfile.TemporaryDirectory() as td:
                path = os.path.join(td, "u.jsonl")
                os.environ["ADA_USAGE_LOG"] = path
                ledger = UsageLedger()
                self.assertEqual(ledger._path, path)
                ledger.record("live", session_id="s", input_tokens=5)
                self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
