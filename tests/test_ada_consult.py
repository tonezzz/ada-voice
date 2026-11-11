import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# In tests, we need to load modules from tools.d properly if they aren't part of standard python package
import importlib.util
spec = importlib.util.spec_from_file_location("ada_consult", "backend/tools.d/ada_consult.py")
ada_consult = importlib.util.module_from_spec(spec)
sys.modules["ada_consult"] = ada_consult
spec.loader.exec_module(ada_consult)


class TestAdaConsult(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runner = MagicMock()
        self.runner.context.emit_ops_event = MagicMock()
        self.runner.mddb = MagicMock()
        self.time_patch = patch('ada_consult.time.monotonic', return_value=100.0)
        self.mock_time = self.time_patch.start()

    async def asyncTearDown(self):
        self.time_patch.stop()

    def _mock_client(self, response_text="expert answer", error=None):
        client = MagicMock()
        generate_mock = AsyncMock()
        if error:
            generate_mock.side_effect = error
        else:
            resp = MagicMock()
            resp.text = response_text
            generate_mock.return_value = resp

        client.aio.models.generate_content = generate_mock
        return client

    @patch('ada_consult.gemini_pool.rest_client')
    async def test_success_call(self, mock_rest_client):
        mock_rest_client.return_value = self._mock_client()

        args = {
            "question": "What is life?",
            "context": "In the universe",
            "consult_model": "gemini-3.5-flash-test"
        }

        res = await ada_consult.run(self.runner, **args)

        self.assertTrue(res["ok"])
        self.assertEqual(res["model"], "gemini-3.5-flash-test")
        self.assertEqual(res["answer"], "expert answer")

        mock_rest_client.assert_called_once()
        self.runner.context.emit_ops_event.assert_any_call(
            ev_type="consult_requested",
            detail="Consult requested for model=gemini-3.5-flash-test, question=What is life?...",
            tool="ada_consult"
        )
        self.runner.context.emit_ops_event.assert_any_call(
            ev_type="consult_completed",
            detail="Consult completed for model=gemini-3.5-flash-test, latency=0.00s",
            tool="ada_consult"
        )

    @patch('ada_consult.gemini_pool.rest_client')
    async def test_failure_call(self, mock_rest_client):
        mock_rest_client.return_value = self._mock_client(error=RuntimeError("API down"))

        args = {
            "question": "What is life?",
            "context": "In the universe",
        }

        res = await ada_consult.run(self.runner, **args)

        self.assertFalse(res["ok"])
        self.assertIn("API down", res["error"])

        self.runner.context.emit_ops_event.assert_any_call(
            ev_type="consult_failed",
            detail="Consult failed for model=gemini-3.5-flash, latency=0.00s: API down",
            tool="ada_consult"
        )

    async def test_missing_args(self):
        res1 = await ada_consult.run(self.runner, context="context")
        self.assertFalse(res1["ok"])
        self.assertEqual(res1["error"], "question is required")

        res2 = await ada_consult.run(self.runner, question="Q")
        self.assertFalse(res2["ok"])
        self.assertEqual(res2["error"], "context is required")

if __name__ == '__main__':
    unittest.main()
