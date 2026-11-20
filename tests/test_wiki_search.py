"""Tests for the wiki_search recall tool (spec door + composed fallback)."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

TOOL_PATH = (Path(__file__).resolve().parent.parent
             / "backend" / "tools.d" / "wiki_search.py")


def _load():
    spec = importlib.util.spec_from_file_location(
        "tools_d.wiki_search_test", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeMddb:
    def __init__(self, docs):
        self._docs = docs

    async def vector_search(self, collection, query, limit=5,
                            filter_meta=None, threshold=0.0):
        assert collection == "chaba-lessons"
        return self._docs


def _runner(mddb=None):
    return SimpleNamespace(context=SimpleNamespace(mddb=mddb))


@pytest.mark.asyncio
async def test_uses_wiki_search_door_when_it_answers():
    tool = _load()
    with patch.object(
        tool.board_client, "post",
        new=AsyncMock(return_value=(
            200, {"results": [
                {"title": "Reports:Weekly-47", "url": "http://w/r",
                 "score": 0.9, "snippet": "s"}]}))) as post:
        result = await tool.run(_runner(), query="weekly report")
    assert result["ok"] is True
    assert result["via"] == "wiki/search"
    assert result["hits"][0]["title"] == "Reports:Weekly-47"
    post.assert_awaited_once_with(
        "/wiki/search",
        {"query": "weekly report", "limit": 8, "from": "ada"},
        timeout=30)


@pytest.mark.asyncio
async def test_fallback_merges_mddb_lessons_and_reports():
    tool = _load()
    mddb = _FakeMddb([
        {"key": "lesson-abc", "score": 0.77,
         "contentMd": "# ada-deploy-flow\n\nbody text",
         "meta": {"wiki_title": ["Education:Ada-Deploy-Flow"],
                  "url": ["http://w/edu"]}}])

    async def fake_post(path, body, timeout=10.0):
        if path == "/wiki/search":
            return 404, {"error": "not found"}
        if path == "/lesson":
            return 200, {"lessons": [
                {"wiki_title": "Education:Ada-Deploy-Flow",
                 "topic": "ada-deploy-flow", "url": "http://w/edu"}]}
        if path == "/report":
            return 200, {"candidates": [
                {"slug": "weekly-fleet", "title": "Weekly fleet report"}]}
        raise AssertionError(path)

    with patch.object(tool.board_client, "post",
                      new=AsyncMock(side_effect=fake_post)):
        result = await tool.run(_runner(mddb), query="ada deploy flow")
    assert result["ok"] is True
    assert result["via"] == "fallback"
    titles = [h["title"] for h in result["hits"]]
    assert "Education:Ada-Deploy-Flow" in titles
    assert "Weekly fleet report" in titles
    sources = {h["title"]: h["source"] for h in result["hits"]}
    assert sources["Education:Ada-Deploy-Flow"] == "education"
    assert sources["Weekly fleet report"] == "report"


@pytest.mark.asyncio
async def test_fallback_dedupes_on_title_keeping_best_score():
    tool = _load()
    mddb = _FakeMddb([
        {"key": "lesson-abc", "score": 0.77,
         "contentMd": "snippet body",
         "meta": {"wiki_title": ["Education:Ada-Deploy-Flow"],
                  "url": ["http://w/edu"]}}])

    async def fake_post(path, body, timeout=10.0):
        if path == "/wiki/search":
            return 404, {"error": "not found"}
        if path == "/lesson":
            return 200, {"lessons": [
                {"wiki_title": "Education:Ada-Deploy-Flow",
                 "topic": "ada-deploy-flow", "url": "http://w/edu"}]}
        return 200, {"candidates": []}

    with patch.object(tool.board_client, "post",
                      new=AsyncMock(side_effect=fake_post)):
        result = await tool.run(_runner(mddb), query="ada deploy")
    hits = [h for h in result["hits"]
            if h["title"] == "Education:Ada-Deploy-Flow"]
    assert len(hits) == 1
    # exact title-token match outranks the vector score; the vector
    # hit's snippet is still carried over on merge
    assert hits[0]["score"] == 1.0
    assert hits[0]["snippet"].startswith("snippet body")


@pytest.mark.asyncio
async def test_fallback_without_mddb_still_searches_doors():
    tool = _load()

    async def fake_post(path, body, timeout=10.0):
        if path == "/wiki/search":
            return 404, {"error": "not found"}
        if path == "/lesson":
            return 200, {"lessons": [
                {"wiki_title": "Education:Ada-Deploy-Flow",
                 "topic": "ada-deploy-flow"}]}
        return 200, {"candidates": []}

    with patch.object(tool.board_client, "post",
                      new=AsyncMock(side_effect=fake_post)):
        result = await tool.run(_runner(None), query="deploy")
    assert result["ok"] is True
    assert result["hits"][0]["title"] == "Education:Ada-Deploy-Flow"


@pytest.mark.asyncio
async def test_board_unreachable_is_fatal_not_empty_fallback():
    tool = _load()
    calls = []

    async def fake_post(path, body, timeout=10.0):
        calls.append(path)
        return 0, {"error": "I couldn't reach the board"}

    with patch.object(tool.board_client, "post",
                      new=AsyncMock(side_effect=fake_post)):
        result = await tool.run(_runner(None), query="x")
    assert result["ok"] is False
    assert calls == ["/wiki/search"]


@pytest.mark.asyncio
async def test_rejects_empty_query():
    tool = _load()
    with patch.object(tool.board_client, "post", new=AsyncMock()) as post:
        result = await tool.run(_runner(), query="  ")
    assert result["ok"] is False
    post.assert_not_awaited()
