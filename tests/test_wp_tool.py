"""Unit tests for backend/tools.d/wp.py — the merged boards-WordPress
writer (card ada-tool-consolidation-merge-overlapping-615ba9). The tool
file is loaded by path (tools.d is not a package); sibling impl modules
are stubbed so no WP REST calls happen."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

TOOL_PATH = (Path(__file__).resolve().parent.parent
             / "backend" / "tools.d" / "wp.py")


def _load():
    spec = importlib.util.spec_from_file_location(
        "tools_d.wp_test", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tool():
    return _load()


@pytest.fixture
def impls(tool, monkeypatch):
    """Stub the four absorbed impl modules wp.py dispatches to."""
    seen = {}

    def fake_loader(name):
        async def _run(runner, **kw):
            seen[name] = kw
            return {"ok": True, "impl": name}
        return SimpleNamespace(run=AsyncMock(side_effect=_run))

    monkeypatch.setattr(tool, "_load_impl", fake_loader)
    return seen


def _runner():
    return SimpleNamespace(instance="kk")


class TestDeclaration:
    def test_declared_name_and_required_action(self, tool):
        assert tool.DECLARATION["name"] == "wp"
        assert tool.DECLARATION["parameters"]["required"] == ["action"]
        actions = tool.DECLARATION["parameters"]["properties"][
            "action"]["enum"]
        assert actions == ["post", "record", "place", "chat_page"]

    def test_union_schema_covers_absorbed_params(self, tool):
        props = tool.DECLARATION["parameters"]["properties"]
        # wp_post
        for k in ("person", "title", "content", "publish", "media_url"):
            assert k in props, k
        # cpt_post
        for k in ("kind", "status", "amount", "vendor", "due_date",
                  "assignee", "model", "serial"):
            assert k in props, k
        # place_report
        for k in ("url", "place", "ask", "lang"):
            assert k in props, k
        # wp_chat_page
        for k in ("member", "lane", "parent", "slug"):
            assert k in props, k


class TestDispatch:
    def test_post_routes_to_wp_post(self, tool, impls):
        out = asyncio.run(tool.run(_runner(), action="post",
                                   person="kk", title="t", content="c"))
        assert out == {"ok": True, "impl": "wp_post"}
        assert impls["wp_post"] == {"person": "kk", "title": "t",
                                    "content": "c"}

    def test_record_routes_to_cpt_post(self, tool, impls):
        out = asyncio.run(tool.run(_runner(), action="record",
                                   kind="contact", title="Somsak"))
        assert out == {"ok": True, "impl": "cpt_post"}
        assert impls["cpt_post"] == {"kind": "contact", "title": "Somsak"}

    def test_place_routes_to_place_report(self, tool, impls):
        out = asyncio.run(tool.run(_runner(), action="place",
                                   url="https://maps.app.goo.gl/x"))
        assert out == {"ok": True, "impl": "place_report"}
        assert impls["place_report"] == {
            "url": "https://maps.app.goo.gl/x"}

    def test_chat_page_routes_to_wp_chat_page(self, tool, impls):
        out = asyncio.run(tool.run(_runner(), action="chat_page",
                                   member="kk",
                                   lane="https://ada-ha.example.com"))
        assert out == {"ok": True, "impl": "wp_chat_page"}
        assert impls["wp_chat_page"] == {
            "member": "kk", "lane": "https://ada-ha.example.com"}

    def test_action_is_not_forwarded_to_impl(self, tool, impls):
        asyncio.run(tool.run(_runner(), action="post", title="t"))
        assert "action" not in impls["wp_post"]

    def test_missing_action_is_ok_false(self, tool, impls):
        out = asyncio.run(tool.run(_runner(), title="t"))
        assert out["ok"] is False
        assert "action" in out["error"]

    def test_unknown_action_is_ok_false(self, tool, impls):
        out = asyncio.run(tool.run(_runner(), action="bogus"))
        assert out["ok"] is False
        assert "bogus" in out["error"]

    def test_impl_result_passes_through(self, tool, monkeypatch):
        mod = SimpleNamespace(run=AsyncMock(return_value={
            "ok": True, "id": 7, "url": "https://boards.example/p/7"}))
        monkeypatch.setattr(tool, "_load_impl", lambda name: mod)
        out = asyncio.run(tool.run(_runner(), action="post"))
        assert out["url"].endswith("/p/7")
