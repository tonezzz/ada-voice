"""Offline tests for scripts/ocb-eval.py and tests/eval_ocb/cases.yml.

No live board/MDDB calls — validates the case schema, the assertion
engine, and that every referenced tool exists on the bridge.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
CASES = REPO / "tests" / "eval_ocb" / "cases.yml"
BRIDGE = REPO / "opencode" / "ada_mcp.py"
EVAL = REPO / "scripts" / "ocb-eval.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cases():
    data = yaml.safe_load(CASES.read_text())
    assert isinstance(data, list) and data, "cases.yml must be a case list"
    return data


def test_case_schema(cases):
    ids = set()
    for case in cases:
        assert case["id"] not in ids, f"duplicate case id {case['id']}"
        ids.add(case["id"])
        assert case.get("prompt"), f"{case['id']} missing prompt"
        expect = case.get("expect")
        assert isinstance(expect, dict) and expect, \
            f"{case['id']} missing expect block"
        assert "tools" in case or "mode" in case, \
            f"{case['id']} needs tools or a live mode"


def test_case_tools_exist_on_bridge(cases):
    """Every tool named in cases.yml must be registered on ada_mcp."""
    eval_mod = _load_module(EVAL, "ocb_eval_test")
    bridge = eval_mod._load_bridge(BRIDGE)
    registered = set(bridge.mcp._tool_manager._tools)
    for case in cases:
        for call in case.get("tools") or []:
            assert call["tool"] in registered, \
                f"{case['id']}: unknown tool {call['tool']}"


def test_assertion_engine_positive():
    eval_mod = _load_module(EVAL, "ocb_eval_assert")
    expect = {
        "answer_contains": ["ada-ocb-board-read-latency"],
        "answer_contains_any": [["review", "column"]],
        "answer_not_contains": ["matched 2 cards"],
        "tools_used": ["ada_status"],
        "mutations_empty": True,
    }
    answer = '{"ok": true, "title": "ada-ocb-board-read-latency", "column": "review", "mutations_performed": []}'
    fails = eval_mod._expect_failures(expect, answer, ["ada_status"], [{}])
    assert fails == []


def test_assertion_engine_negative():
    eval_mod = _load_module(EVAL, "ocb_eval_assert_neg")
    expect = {
        "answer_contains": ["missing-needle"],
        "answer_contains_any": [["nope-a", "nope-b"]],
        "answer_not_contains": ["forbidden"],
        "tools_used": ["ada_status"],
        "tools_not_used": ["ada_ssot_get"],
    }
    answer = "forbidden needle present"
    docs = [{"ok": False, "error": "boom"}]
    fails = eval_mod._expect_failures(
        expect, answer, ["ada_ssot_get"], docs)
    assert any("missing-needle" in f for f in fails)
    assert any("nope-a" in f for f in fails)
    assert any("forbidden" in f for f in fails)
    assert any("ada_status" in f and "not used" in f for f in fails)
    assert any("ada_ssot_get" in f and "unexpectedly" in f for f in fails)
    assert any("ok=false" in f for f in fails)


def test_mutations_detected():
    eval_mod = _load_module(EVAL, "ocb_eval_mut")
    expect = {"mutations_empty": True}
    docs = [{"ok": True, "mutations_performed": [{"op": "queue"}]}]
    fails = eval_mod._expect_failures(expect, "{}", [], docs)
    assert any("mutation leaked" in f for f in fails)


def test_run_case_tools_offline(monkeypatch, tmp_path):
    """_run_case_tools against a stub bridge with canned tool results."""
    eval_mod = _load_module(EVAL, "ocb_eval_run")

    class _Tool:
        def __init__(self, fn):
            self.fn = fn

    class _Mgr:
        _tools = {
            "ada_status": _Tool(lambda **kw: '{"ok": true, "column": "review", "mutations_performed": []}'),
        }

    class _MCP:
        _tool_manager = _Mgr()

    class FakeBridge:
        mcp = _MCP()

    case = {
        "id": "stub", "prompt": "p",
        "tools": [{"tool": "ada_status", "args": {"card_reference": "X"}}],
        "expect": {"answer_contains": ["review"], "tools_used": ["ada_status"]},
    }
    rec = eval_mod._run_case_tools(case, FakeBridge())
    assert rec["ok"] is True
    assert rec["fails"] == []

    bad = dict(case, expect={"answer_contains": ["done-column"]})
    rec2 = eval_mod._run_case_tools(bad, FakeBridge())
    assert rec2["ok"] is False
    assert any("done-column" in f for f in rec2["fails"])
