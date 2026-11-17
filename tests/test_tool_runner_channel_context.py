import asyncio
import unittest
from unittest.mock import patch

from backend.tool_runner import ToolRunner


class TestToolRunnerChannelContext(unittest.TestCase):
    def test_channel_is_scoped_to_provider_tool_call(self):
        runner = object.__new__(ToolRunner)
        runner.mddb = None
        runner.ada_consult = lambda **_: None

        async def capture_channel(tool_runner, *args, **kwargs):
            return tool_runner.current_channel

        with patch.object(ToolRunner, "_storm_check", return_value=None), \
                patch.object(ToolRunner, "_execute_gated",
                             new=capture_channel):
            channel = asyncio.run(runner.execute(
                "ada_consult", {}, identity=None, channel="webchat"))
            unscoped = asyncio.run(runner.execute(
                "ada_consult", {}, identity=None))

        self.assertEqual(channel, "webchat")
        self.assertIsNone(unscoped)
        self.assertIsNone(runner.current_channel)
