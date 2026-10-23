"""Ensure Telegram Bot API credentials cannot leak through request logs."""

from __future__ import annotations

import importlib
import logging
import sys
import types
import unittest
from unittest.mock import patch

# This test exercises logging helpers only; relay I/O dependencies are not used.
for _module in ("httpx", "websockets"):
    try:
        importlib.import_module(_module)
    except ModuleNotFoundError:
        sys.modules[_module] = types.ModuleType(_module)

from backend import tg_relay


class TelegramTokenLoggingTests(unittest.TestCase):
    token = "synthetic-test-token/never-log"

    def setUp(self):
        self.redactor = tg_relay._TelegramTokenRedactionFilter(self.token)

    def test_redacts_token_from_formatted_message(self):
        record = logging.LogRecord(
            "httpx", logging.INFO, __file__, 1,
            "HTTP Request: POST https://api.telegram.org/bot%s/sendMessage",
            (self.token,), None)

        self.assertTrue(self.redactor.filter(record))
        self.assertNotIn(self.token, record.getMessage())
        self.assertIn("[REDACTED]", record.getMessage())

    def test_redacts_token_from_exception_traceback(self):
        try:
            raise RuntimeError(
                f"request failed: https://api.telegram.org/bot{self.token}/getMe")
        except RuntimeError:
            exc_info = sys.exc_info()
        record = logging.LogRecord(
            "tg_relay", logging.WARNING, __file__, 1,
            "poll failed", (), exc_info)

        self.assertTrue(self.redactor.filter(record))
        rendered = logging.Formatter().format(record)
        self.assertNotIn(self.token, rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_httpx_request_trace_is_suppressed_and_warnings_are_redacted(self):
        httpx_logger = logging.getLogger("httpx")
        original_level = httpx_logger.level
        with patch.object(tg_relay, "BOT_TOKEN", self.token):
            installed = tg_relay._install_telegram_log_safety()
        try:
            self.assertIsNotNone(installed)
            self.assertGreaterEqual(httpx_logger.level, logging.WARNING)
            with self.assertLogs("httpx", level=logging.WARNING) as captured:
                httpx_logger.info(
                    "HTTP Request: POST "
                    f"https://api.telegram.org/bot{self.token}/sendMessage")
                httpx_logger.warning(
                    "request failed: "
                    f"https://api.telegram.org/bot{self.token}/getMe")
            output = "\n".join(captured.output)
            self.assertNotIn(self.token, output)
            self.assertIn("[REDACTED]", output)
            self.assertNotIn("HTTP Request:", output)
        finally:
            for log in (logging.getLogger(), tg_relay.logger,
                        logging.getLogger("httpx"),
                        logging.getLogger("httpcore")):
                if installed:
                    log.filters[:] = [f for f in log.filters
                                      if f is not installed]
                    for handler in log.handlers:
                        handler.filters[:] = [
                            f for f in handler.filters if f is not installed]
            httpx_logger.setLevel(original_level)


if __name__ == "__main__":
    unittest.main()
