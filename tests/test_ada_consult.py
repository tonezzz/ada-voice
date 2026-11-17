import asyncio
import importlib.util
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

spec = importlib.util.spec_from_file_location(
    "ada_consult", "backend/tools.d/ada_consult.py")
ada_consult = importlib.util.module_from_spec(spec)
sys.modules["ada_consult"] = ada_consult
spec.loader.exec_module(ada_consult)


class TestAdaConsult(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runner = MagicMock()
        self.runner.context.emit_ops_event = MagicMock()
        self.runner.current_channel = "webchat"

    def _mock_client(self, response_text="expert answer", error=None):
        client = MagicMock()
        generate = AsyncMock()
        if error:
            generate.side_effect = error
        else:
            response = MagicMock()
            response.text = response_text
            response.usage_metadata = SimpleNamespace(
                prompt_token_count=12,
                candidates_token_count=20,
                total_token_count=32,
            )
            generate.return_value = response
        client.aio.models.generate_content = generate
        return client

    def _gateway_env(self):
        return patch.dict(os.environ, {
            "ADA_GEMINI_REST_BASE_URL": "https://gateway.invalid/v1beta",
            "LITELLM_KEY_CONSULT": "test-consult-key",
            "ADA_CONSULT_MAX_USD": "0.10",
            "ADA_CONSULT_FLASH_INPUT_USD_PER_MILLION": "1",
            "ADA_CONSULT_FLASH_OUTPUT_USD_PER_MILLION": "2",
        })

    @patch("ada_consult.gemini_pool.rest_client")
    async def test_success_uses_dedicated_gateway_key_and_safe_events(
        self, mock_rest_client,
    ):
        mock_rest_client.return_value = self._mock_client()
        with self._gateway_env():
            result = await ada_consult.run(
                self.runner, question="What is life?", context="In the universe",
                context_class="curated",
                consult_model="consult-flash")

        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "consult-flash")
        self.assertEqual(result["answer"], "expert answer")
        self.assertEqual(result["usage"]["total_tokens"], 32)
        self.assertEqual(result["estimated_cost_usd"], 0.000052)
        mock_rest_client.assert_called_once_with(
            gateway_api_key="test-consult-key")
        call = mock_rest_client.return_value.aio.models.generate_content
        self.assertEqual(call.call_args.kwargs["model"], "consult-flash")
        self.assertIn("untrusted reference context",
                      call.call_args.kwargs["contents"])
        self.assertEqual(call.call_args.kwargs["config"].max_output_tokens, 3072)
        event_details = [
            call.kwargs["detail"]
            for call in self.runner.context.emit_ops_event.call_args_list
        ]
        self.assertTrue(any("question_sha256=" in detail
                            for detail in event_details))
        self.assertFalse(any("What is life?" in detail
                             for detail in event_details))

    @patch("ada_consult.gemini_pool.rest_client")
    async def test_failure_event_does_not_include_provider_exception_text(
        self, mock_rest_client,
    ):
        mock_rest_client.return_value = self._mock_client(
            error=RuntimeError("request included secret text"))
        with self._gateway_env():
            result = await ada_consult.run(
                self.runner, question="Question", context="Curated context",
                context_class="curated")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "consultation failed (RuntimeError)")
        details = [
            call.kwargs["detail"]
            for call in self.runner.context.emit_ops_event.call_args_list
        ]
        self.assertFalse(any("secret text" in detail for detail in details))

    @patch("ada_consult.gemini_pool.rest_client")
    async def test_secret_and_transcript_inputs_are_refused(self, mock_rest_client):
        with self._gateway_env():
            secret = await ada_consult.run(
                self.runner, question="Question",
                context="api_key=not-a-real-secret-value",
                context_class="curated")
            transcript = await ada_consult.run(
                self.runner, question="Question",
                context=("=== MESSAGE 1 - User ===\nhello\n"
                         "=== MESSAGE 2 - Assistant ===\nanswer"),
                context_class="curated")

        self.assertFalse(secret["ok"])
        self.assertIn("refused", secret["error"])
        self.assertFalse(transcript["ok"])
        self.assertIn("transcript", transcript["error"])
        mock_rest_client.assert_not_called()

    @patch("ada_consult.gemini_pool.rest_client")
    async def test_explicit_private_transcript_class_is_refused(
        self, mock_rest_client,
    ):
        with self._gateway_env():
            result = await ada_consult.run(
                self.runner, question="Question", context="Curated excerpt",
                context_class="private_transcript")
        self.assertFalse(result["ok"])
        self.assertIn("transcript", result["error"])
        mock_rest_client.assert_not_called()

    @patch("ada_consult.gemini_pool.rest_client")
    async def test_requires_gateway_key_and_allowlisted_alias(
        self, mock_rest_client,
    ):
        with patch.dict(os.environ, {
            "ADA_CONSULT_MAX_USD": "0.10",
            "ADA_CONSULT_FLASH_INPUT_USD_PER_MILLION": "1",
            "ADA_CONSULT_FLASH_OUTPUT_USD_PER_MILLION": "2",
        }, clear=True):
            unconfigured = await ada_consult.run(
                self.runner, question="Question", context="Curated context",
                context_class="curated")
        with self._gateway_env():
            raw_model = await ada_consult.run(
                self.runner, question="Question", context="Curated context",
                context_class="curated",
                consult_model="gemini-3.5-flash")

        self.assertFalse(unconfigured["ok"])
        self.assertIn("not configured", unconfigured["error"])
        self.assertFalse(raw_model["ok"])
        self.assertIn("allowed consult alias", raw_model["error"])
        mock_rest_client.assert_not_called()

    @patch("ada_consult.gemini_pool.rest_client")
    async def test_voice_channel_is_refused(self, mock_rest_client):
        self.runner.current_channel = None
        with self._gateway_env():
            result = await ada_consult.run(
                self.runner, question="Question", context="Curated context",
                context_class="curated")
        self.assertFalse(result["ok"])
        self.assertIn("text-only", result["error"])
        mock_rest_client.assert_not_called()

    @patch("ada_consult.gemini_pool.rest_client")
    async def test_input_size_is_bounded(self, mock_rest_client):
        with self._gateway_env(), patch.dict(
            os.environ, {"ADA_CONSULT_MAX_CONTEXT_CHARS": "3"}):
            result = await ada_consult.run(
                self.runner, question="Q", context="long context",
                context_class="curated")
        self.assertFalse(result["ok"])
        self.assertIn("size limit", result["error"])
        mock_rest_client.assert_not_called()

    @patch("ada_consult.gemini_pool.rest_client")
    async def test_per_call_cost_bound_prevents_expensive_request(
        self, mock_rest_client,
    ):
        with self._gateway_env(), patch.dict(os.environ, {
            "ADA_CONSULT_MAX_USD": "0.000001",
        }):
            result = await ada_consult.run(
                self.runner, question="Question", context="Curated context",
                context_class="curated")
        self.assertFalse(result["ok"])
        self.assertIn("ADA_CONSULT_MAX_USD", result["error"])
        mock_rest_client.assert_not_called()

    @patch("ada_consult.gemini_pool.rest_client")
    async def test_wall_time_is_bounded(self, mock_rest_client):
        client = self._mock_client(error=asyncio.TimeoutError())
        mock_rest_client.return_value = client
        with self._gateway_env():
            result = await ada_consult.run(
                self.runner, question="Question", context="Curated context",
                context_class="curated")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "consultation timed out")

    async def test_context_class_is_required(self):
        result = await ada_consult.run(
            self.runner, question="Question", context="Curated context")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "context_class is required")

    def test_cost_estimate_requires_configured_rates(self):
        usage = {"input_tokens": 1000, "output_tokens": 2000}
        with patch.dict(os.environ, {
            "ADA_CONSULT_FLASH_INPUT_USD_PER_MILLION": "1",
            "ADA_CONSULT_FLASH_OUTPUT_USD_PER_MILLION": "2",
        }):
            self.assertEqual(
                ada_consult._estimate_cost(usage, "consult-flash"), 0.005)


if __name__ == "__main__":
    unittest.main()
