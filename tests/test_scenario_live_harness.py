"""Unit tests for check_turn's lane-gate-aware call assertions.

The Live provider emits `tool_call` before the _tool_excluded gate, so a
refused attempt still appears in the event stream. `no_calls_except` keeps
its old (strict) semantics for gate scenarios; the new
`no_successful_calls_except` ignores calls whose result was a lane denial.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/scenario-live.py"
_spec = importlib.util.spec_from_file_location("scenario_live", SCRIPT)
sl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sl)


def _events(call_name: str, result: dict) -> list[dict]:
    return [
        {"type": "tool_call", "id": "c1", "name": call_name},
        {"type": "tool_result", "id": "c1", "name": call_name,
         "result": result},
        {"type": "assistant_transcript_delta", "text": "hi"},
    ]


DENIED = {"error": ("ada_session_recall is not available on this lane — "
                    "answer with the tools you have.")}


def test_denied_call_does_not_fail_new_assertion() -> None:
    events = _events("ada_session_recall", DENIED)
    assert sl.check_turn(events, {
        "no_successful_calls_except": ["set_facial_expression", "cms_read"],
        "response_nonempty": True,
    }) == []


def test_denied_call_still_fails_old_assertion() -> None:
    events = _events("ada_session_recall", DENIED)
    failures = sl.check_turn(events, {
        "no_calls": True,
        "no_calls_except": ["set_facial_expression", "cms_read"],
    })
    assert any("no_calls: got ['ada_session_recall']" in f for f in failures)


def test_successful_barred_call_fails_new_assertion() -> None:
    events = _events("ada_memory_search", {"ok": True, "hits": []})
    failures = sl.check_turn(events, {
        "no_successful_calls_except": ["set_facial_expression", "cms_read"],
    })
    assert any("no_successful_calls_except" in f for f in failures)


def test_seed_payload_defaults() -> None:
    body = sl.seed_payload({
        "collection": "ada-cms-pages-kk-dev",
        "key": "zeta-kk-cms-7741",
        "content_md": "seeded body",
    })
    assert body["lang"] == "en"
    assert body["meta"] == {}
    assert body["content_md"] == "seeded body"
    norm = sl.seed_payload({
        "collection": "c", "key": "k", "meta": {"kind": "report",
                                                "tags": ["a", "b"]},
    })
    assert norm["meta"] == {"kind": ["report"], "tags": ["a", "b"]}


def test_response_contains_all_any() -> None:
    events = [
        {"type": "assistant_transcript_delta",
         "text": "รดน้ำวันจันทร์และพฤหัส ต้องล่อน้ำปั๊มก่อน"},
    ]
    assert sl.check_turn(events, {
        "response_contains_all_any": [
            ["ปั๊ม", "pump"], ["จันทร์", "Monday"], ["พฤหัส", "Thursday"],
        ],
        "response_not_contains": ["อังคาร", "Tuesday"],
    }) == []
    fails = sl.check_turn(events, {
        "response_contains_all_any": [["อังคาร", "Tuesday"]],
    })
    assert any("response_contains_all_any" in f for f in fails)


def test_seed_verified_requires_exact_key_and_lang() -> None:
    docs = [{"key": "zeta-kk-cms-7741", "lang": "en"},
            {"key": "zeta-kk-cms-7741", "lang": "th"}]
    assert sl.seed_verified(docs, "zeta-kk-cms-7741", "en") is True
    assert sl.seed_verified(docs, "zeta-kk-cms-7741", "de") is False
    assert sl.seed_verified(docs, "other", "en") is False
