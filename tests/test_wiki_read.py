"""Tests for the board-api-door wiki_read tool."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

TOOL_PATH = (Path(__file__).resolve().parent.parent
             / "backend" / "tools.d" / "wiki_read.py")


def _load():
    spec = importlib.util.spec_from_file_location(
        "tools_d.wiki_read_test", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_uses_page_door_when_it_answers():
    """Once board-api serves /wiki/page mode=read it wins outright."""
    tool = _load()
    with patch.object(
        tool.board_client, "post",
        new=AsyncMock(return_value=(
            200, {"title": "Reports:Weekly", "wikitext": "== body ==",
                  "url": "http://wiki/x"}))) as post:
        result = await tool.run(SimpleNamespace(), title="Reports:Weekly")
    assert result["ok"] is True
    assert result["wikitext"] == "== body =="
    assert result["via"] == "wiki/page"
    post.assert_awaited_once_with(
        "/wiki/page",
        {"mode": "read", "title": "Reports:Weekly", "from": "ada"},
        timeout=30)


@pytest.mark.asyncio
async def test_education_falls_back_to_lesson_door():
    """mode=read unsupported (400 'wikitext required') -> /lesson get."""
    tool = _load()
    calls = []

    async def fake_post(path, body, timeout=10.0):
        calls.append(path)
        if path == "/wiki/page":
            return 400, {"error": "wikitext required"}
        return 200, {"wiki_title": "Education:Ada-Deploy-Flow",
                     "content": "lesson body",
                     "url": "http://wiki/e",
                     "meta": {"level": "working"},
                     "source": "wiki"}

    with patch.object(tool.board_client, "post",
                      new=AsyncMock(side_effect=fake_post)):
        result = await tool.run(
            SimpleNamespace(), title="Education:Ada-Deploy-Flow")
    assert result["ok"] is True
    assert result["wikitext"] == "lesson body"
    assert result["via"] == "lesson"
    assert calls == ["/wiki/page", "/lesson"]


@pytest.mark.asyncio
async def test_private_news_falls_back_to_review_door():
    tool = _load()

    async def fake_post(path, body, timeout=10.0):
        if path == "/wiki/page":
            return 400, {"error": "wikitext required"}
        assert path == "/wiki/news-review"
        return 200, {"wiki_title": body["wiki_title"],
                     "wikitext": "draft body", "revid": 42}

    with patch.object(tool.board_client, "post",
                      new=AsyncMock(side_effect=fake_post)):
        result = await tool.run(
            SimpleNamespace(), title="PrivateNews:Draft")
    assert result["ok"] is True
    assert result["wikitext"] == "draft body"
    assert result["via"] == "news-review"


@pytest.mark.asyncio
async def test_uncovered_namespace_errors_honestly():
    tool = _load()
    with patch.object(
        tool.board_client, "post",
        new=AsyncMock(return_value=(400, {"error": "wikitext required"}))):
        result = await tool.run(SimpleNamespace(), title="Reports:Weekly")
    assert result["ok"] is False
    assert "Education:*" in result["error"]


@pytest.mark.asyncio
async def test_missing_education_page_reports_not_found():
    tool = _load()

    async def fake_post(path, body, timeout=10.0):
        if path == "/wiki/page":
            return 400, {"error": "wikitext required"}
        return 400, {"error": "lesson not found: Education:Nope"}

    with patch.object(tool.board_client, "post",
                      new=AsyncMock(side_effect=fake_post)):
        result = await tool.run(SimpleNamespace(),
                                title="Education:Nope")
    assert result["ok"] is False
    assert "wiki_search" in result["error"]


@pytest.mark.asyncio
async def test_board_unreachable_is_fatal_not_fallback():
    tool = _load()
    calls = []

    async def fake_post(path, body, timeout=10.0):
        calls.append(path)
        return 0, {"error": "I couldn't reach the board"}

    with patch.object(tool.board_client, "post",
                      new=AsyncMock(side_effect=fake_post)):
        result = await tool.run(SimpleNamespace(), title="Education:X")
    assert result["ok"] is False
    assert calls == ["/wiki/page"]


@pytest.mark.asyncio
async def test_rejects_bad_title_and_mode():
    tool = _load()
    with patch.object(tool.board_client, "post", new=AsyncMock()) as post:
        r1 = await tool.run(SimpleNamespace(), title="")
        r2 = await tool.run(SimpleNamespace(), title="A\nB")
        r3 = await tool.run(SimpleNamespace(), title="Education:X",
                            mode="delete")
    assert r1["ok"] is False and r2["ok"] is False
    assert r3["ok"] is False and "mode" in r3["error"]
    post.assert_not_awaited()
