"""tool_budget (card ada-spend-guardrails-per-tool-daily-caps): per-tool
daily spend caps enforced at the runner gate and the gemini_pool aux-call
pre-flight, plus the ada_ops action='budget' readout. Spend is summed
from ADA_USAGE_LOG rows for the UTC day, keyed by rec['tool'] (falling
back to rec['source'] on older rows).
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GEMINI_API_KEY", "x")

from backend import gemini_pool, tool_budget  # noqa: E402
from backend.tool_budget import ToolBudgetExceeded  # noqa: E402
from backend.usage_tracker import usage_ledger  # noqa: E402


def _usage_file(rows):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for rec in rows:
            f.write(json.dumps(rec) + "\n")
    return path


def _row(tool=None, source="gemini_aux", cost=0.0, days_ago=0,
         in_tokens=0, out_tokens=0, **kw):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)
          ).isoformat()
    rec = {"ts": ts, "source": source, "in": in_tokens, "out": out_tokens,
           "in_mod": {}, "out_mod": {}, "cached": 0, "tool_use": 0,
           "cost": cost, "lane": "test"}
    if tool is not None:
        rec["tool"] = tool
    rec.update(kw)
    return rec


class ToolBudgetCase(unittest.TestCase):
    def setUp(self):
        tool_budget.reset()
        self._tmp = tempfile.mkdtemp()
        self.usage_path = str(Path(self._tmp) / "usage.jsonl")
        self._env = patch.dict(os.environ, {
            "ADA_USAGE_LOG": self.usage_path,
            "ADA_TOOL_DAILY_CAP_DEFAULT_USD": "",
        })
        self._env.start()
        for var in list(os.environ):
            if var.startswith("ADA_TOOL_DAILY_CAP_") and var != (
                    "ADA_TOOL_DAILY_CAP_DEFAULT_USD"):
                os.environ.pop(var, None)

    def tearDown(self):
        self._env.stop()
        tool_budget.reset()

    def _write(self, rows):
        Path(self.usage_path).write_text(
            "".join(json.dumps(r) + "\n" for r in rows))
        tool_budget.reset()


class SpendScanTests(ToolBudgetCase):
    def test_day_spend_sums_tool_key_and_source_fallback(self):
        self._write([
            _row(tool="web_search", in_tokens=1_000_000,
                 in_mod={"text": 1_000_000}),                    # $0.30
            _row(source="render_video", cost=1.25),        # flat cost, no tool
            _row(tool="web_search", days_ago=1,
                 in_tokens=9_000_000),                    # yesterday
        ])
        # text in-rate $0.30/M -> 1M tokens = $0.30
        self.assertAlmostEqual(tool_budget.day_spend_usd("web_search"),
                               0.30, places=3)
        self.assertAlmostEqual(
            tool_budget.day_spend_usd("ada_render_video"), 1.25, places=3)
        # canonical name pulls in its alias labels both ways
        self.assertAlmostEqual(
            tool_budget.day_spend_usd("render_video"), 1.25, places=3)

    def test_alias_label_aggregates_canonical_set(self):
        self._write([
            _row(tool="render_image", cost=0.10),
            _row(tool="render_check", cost=0.05),
        ])
        self.assertAlmostEqual(
            tool_budget.day_spend_usd("ada_render_image"), 0.15, places=3)
        self.assertAlmostEqual(
            tool_budget.day_spend_usd("render_image"), 0.15, places=3)

    def test_unknown_tool_spends_under_own_key(self):
        self._write([_row(tool="mystery_tool", cost=0.42)])
        self.assertAlmostEqual(
            tool_budget.day_spend_usd("mystery_tool"), 0.42, places=3)


class CapConfigTests(ToolBudgetCase):
    def test_uncapped_by_default(self):
        self.assertEqual(tool_budget.cap_usd("web_search"), 0.0)
        self.assertIsNone(tool_budget.exceeded("web_search"))

    def test_env_cap_wins(self):
        with patch.dict(os.environ,
                        {"ADA_TOOL_DAILY_CAP_WEB_SEARCH": "0.5"}):
            self.assertEqual(tool_budget.cap_usd("web_search"), 0.5)

    def test_canonical_env_cap_covers_alias_label(self):
        with patch.dict(os.environ,
                        {"ADA_TOOL_DAILY_CAP_ADA_RENDER_IMAGE": "1.5"}):
            self.assertEqual(tool_budget.cap_usd("render_image"), 1.5)
            self.assertEqual(tool_budget.cap_usd("ada_render_image"), 1.5)

    def test_manifest_cap_for_tools_d(self):
        # manifest.yml ships daily_cap_usd for the render tools
        self.assertEqual(tool_budget.cap_usd("ada_render_video"), 5.0)
        self.assertEqual(tool_budget.cap_usd("ada_render_image"), 2.0)
        # env overrides manifest
        with patch.dict(os.environ,
                        {"ADA_TOOL_DAILY_CAP_ADA_RENDER_VIDEO": "9"}):
            self.assertEqual(tool_budget.cap_usd("ada_render_video"), 9.0)

    def test_default_cap_catch_all(self):
        with patch.dict(os.environ,
                        {"ADA_TOOL_DAILY_CAP_DEFAULT_USD": "0.25"}):
            self.assertEqual(tool_budget.cap_usd("anything"), 0.25)
            # explicit per-tool env still wins over the default
            with patch.dict(os.environ,
                            {"ADA_TOOL_DAILY_CAP_ANYTHING": "2"}):
                self.assertEqual(tool_budget.cap_usd("anything"), 2.0)


class EnforcementTests(ToolBudgetCase):
    def test_check_returns_denial_over_cap(self):
        self._write([_row(tool="web_search", cost=0.60)])
        with patch.dict(os.environ, {"ADA_TOOL_DAILY_CAP_WEB_SEARCH": "0.5"}):
            hit = tool_budget.check("web_search")
        self.assertIsNotNone(hit)
        self.assertFalse(hit["ok"])
        self.assertEqual(hit["error_type"], "ToolBudgetExceeded")
        self.assertEqual(hit["budget"]["cap_usd"], 0.5)
        self.assertGreaterEqual(hit["budget"]["spent_usd"], 0.5)

    def test_check_none_under_cap_and_uncapped(self):
        self._write([_row(tool="web_search", cost=0.10)])
        with patch.dict(os.environ, {"ADA_TOOL_DAILY_CAP_WEB_SEARCH": "0.5"}):
            self.assertIsNone(tool_budget.check("web_search"))
        self.assertIsNone(tool_budget.check("other_tool"))

    def test_check_key_raises(self):
        self._write([_row(tool="render_image", cost=3.0)])
        with self.assertRaises(ToolBudgetExceeded) as ctx:
            # manifest cap for ada_render_image is $2 — label resolves
            with patch.dict(os.environ, {}):
                tool_budget.check_key("render_image")
        self.assertEqual(ctx.exception.key, "render_image")
        self.assertGreaterEqual(ctx.exception.spent, 2.0)

    def test_runner_gate_short_circuits_tool(self):
        """execute() returns the budget denial before the tool runs."""
        from backend.tool_runner import ToolRunner
        self._write([_row(tool="get_home_state", cost=1.0)])
        runner = ToolRunner(AsyncMock(), instance_id="test")
        runner._vcast_api = lambda *a, **k: {"captures": {}}
        with patch.dict(os.environ,
                        {"ADA_TOOL_DAILY_CAP_GET_HOME_STATE": "0.5"}):
            result = asyncio.run(runner.execute("get_home_state", {}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "ToolBudgetExceeded")
        self.assertIn("daily spend cap", result["error"])

    def test_runner_gate_allows_under_cap(self):
        from backend.tool_runner import ToolRunner
        self._write([_row(tool="get_home_state", cost=0.10)])
        runner = ToolRunner(AsyncMock(), instance_id="test")
        runner._vcast_api = lambda *a, **k: {"captures": {}}
        runner._banks = None  # not reached for this read path
        with patch.dict(os.environ,
                        {"ADA_TOOL_DAILY_CAP_GET_HOME_STATE": "0.5"}):
            # method runs — whatever it returns, it is NOT the budget dict
            result = asyncio.run(runner.execute("get_home_state", {}))
        self.assertNotEqual(
            (result or {}).get("error_type") if isinstance(result, dict)
            else None, "ToolBudgetExceeded")


class ReadoutTests(ToolBudgetCase):
    def test_status_reports_spend_caps_and_remaining(self):
        self._write([
            _row(tool="render_image", cost=0.50),
            _row(tool="render_check", cost=0.25),
            _row(tool="web_search", cost=0.10),
            _row(source="live", in_tokens=2_000_000,
                 out_tokens=500_000),
        ])
        with patch.dict(os.environ,
                        {"ADA_TOOL_DAILY_CAP_ADA_RENDER_IMAGE": "2.0"}):
            st = tool_budget.status()
        self.assertEqual(st["day"], tool_budget.day())
        # alias labels aggregate onto the canonical tool
        self.assertAlmostEqual(
            st["spend_by_tool"]["ada_render_image"], 0.75, places=3)
        self.assertAlmostEqual(st["spend_by_tool"]["web_search"], 0.10,
                               places=3)
        capped = st["capped"]["ada_render_image"]
        self.assertEqual(capped["cap_usd"], 2.0)
        self.assertAlmostEqual(capped["spent_usd"], 0.75, places=3)
        self.assertAlmostEqual(capped["remaining_usd"], 1.25, places=3)
        # uncapped tools show spend but no cap entry
        self.assertIn("web_search", st["spend_by_tool"])
        self.assertNotIn("web_search", st["capped"])

    def test_ops_budget_action_returns_readout(self):
        from backend.tool_runner import ToolRunner
        self._write([_row(tool="web_search", cost=0.10)])
        runner = ToolRunner(AsyncMock(), instance_id="test")
        runner._vcast_api = lambda *a, **k: {"captures": {}}
        result = asyncio.run(runner.execute("ada_ops", {"action": "budget"}))
        self.assertIn("spend_by_tool", result)
        self.assertIn("capped", result)
        self.assertEqual(result["day"], tool_budget.day())


class MeteringTests(ToolBudgetCase):
    def test_record_usage_writes_tool_label(self):
        usage_ledger.configure(self.usage_path)
        resp = SimpleNamespace(usage_metadata=SimpleNamespace(
            prompt_token_count=1000, candidates_token_count=200,
            cached_content_token_count=0,
            prompt_tokens_details=[], candidates_tokens_details=[]))
        gemini_pool._record_usage(resp, "web_search", "sess-1")
        rows = [json.loads(l) for l in
                Path(self.usage_path).read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool"], "web_search")
        self.assertEqual(rows[0]["source"], "gemini_aux")
        self.assertEqual(rows[0]["in"], 1000)
        self.assertEqual(rows[0]["out"], 200)
        usage_ledger.configure(None)

    def test_record_usage_never_raises_on_garbage(self):
        gemini_pool._record_usage(object(), "web_search")
        gemini_pool._record_usage(
            SimpleNamespace(usage_metadata=object()), "web_search")

    def test_generate_blocked_over_cap_before_any_call(self):
        self._write([_row(tool="web_search", cost=0.60)])
        with patch.dict(os.environ,
                        {"ADA_TOOL_DAILY_CAP_WEB_SEARCH": "0.5"}):
            with self.assertRaises(ToolBudgetExceeded):
                asyncio.run(gemini_pool.generate(
                    "gemini-3.5-flash-lite", "hi", tool="web_search"))


if __name__ == "__main__":
    unittest.main()
