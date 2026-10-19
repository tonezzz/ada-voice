"""Unit tests for backend/tools.d/weather_station.py — module loaded by
path (tools.d is not a package). RikaCloud HTTP is mocked at _latest;
these cover field scaling, compass mapping, and the stale-data honesty
flag — not the wire."""

import importlib.util
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

TOOL_PATH = (Path(__file__).resolve().parent.parent
             / "backend" / "tools.d" / "weather_station.py")


def _load():
    spec = importlib.util.spec_from_file_location(
        "tools_d.weather_station_test", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tool():
    return _load()


def _row(e2=0, e5=0, age_s=0, e1=90, e3=283, e4=846):
    dt = datetime.now(timezone(timedelta(hours=8))) - timedelta(
        seconds=age_s)
    return {"dataTime": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "e1": e1, "e2": e2, "e3": e3, "e4": e4, "e5": e5,
            "facId": 22100502}


class TestCompass:
    def test_cardinals(self, tool):
        assert tool._compass(0) == "N"
        assert tool._compass(90) == "E"
        assert tool._compass(180) == "S"
        assert tool._compass(270) == "W"

    def test_intercardinal_and_wrap(self, tool):
        assert tool._compass(45) == "NE"
        assert tool._compass(350) == "N"


class TestRun:
    @pytest.mark.asyncio
    async def test_fresh_reading(self, tool):
        with patch.object(tool, "_load_env",
                          return_value={"RIKA_ACCOUNT": "a",
                                        "RIKA_PASSWORD": "b"}), \
             patch.object(tool, "_latest",
                          new=AsyncMock(return_value=_row(e2=772))):
            out = await tool.run(SimpleNamespace(instance="kraut"))
        assert out["ok"] is True
        assert out["stale"] is False
        assert out["wind_kt"] == pytest.approx(15.0, abs=0.05)
        assert out["wind_dir"] == "E"
        assert out["temp_c"] == 28.3
        assert out["humidity_pct"] == 84.6
        assert out["raining"] is False

    @pytest.mark.asyncio
    async def test_stale_flags_age(self, tool):
        with patch.object(tool, "_load_env",
                          return_value={"RIKA_ACCOUNT": "a",
                                        "RIKA_PASSWORD": "b"}), \
             patch.object(tool, "_latest",
                          new=AsyncMock(return_value=_row(age_s=7200))):
            out = await tool.run(SimpleNamespace(instance="kraut"))
        assert out["ok"] is True
        assert out["stale"] is True
        assert out["age_min"] >= 119
        assert "stale" in out["note"]

    @pytest.mark.asyncio
    async def test_rain_active(self, tool):
        with patch.object(tool, "_load_env",
                          return_value={"RIKA_ACCOUNT": "a",
                                        "RIKA_PASSWORD": "b"}), \
             patch.object(tool, "_latest",
                          new=AsyncMock(return_value=_row(e5=191))):
            out = await tool.run(SimpleNamespace(instance="kraut"))
        assert out["raining"] is True
        assert out["rain"] == 191

    @pytest.mark.asyncio
    async def test_missing_env(self, tool):
        with patch.object(tool, "_load_env", return_value={}):
            out = await tool.run(SimpleNamespace(instance="kraut"))
        assert out["ok"] is False
        assert "rikacloud.env" in out["error"]

    @pytest.mark.asyncio
    async def test_http_error_narrates(self, tool):
        with patch.object(tool, "_load_env",
                          return_value={"RIKA_ACCOUNT": "a",
                                        "RIKA_PASSWORD": "b"}), \
             patch.object(tool, "_latest",
                          new=AsyncMock(side_effect=TimeoutError("x"))):
            out = await tool.run(SimpleNamespace(instance="kraut"))
        assert out["ok"] is False
        assert "TimeoutError" in out["error"]
