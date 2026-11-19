"""Unit tests for opencode/ada_mcp.py native-menu selection gate.

The pick -> confirm -> act flow must only act on cards that were
actually offered by ada_next_card/ada_card_pick, with the per-card
selection_token that was issued. Covers _selection_gate states and
the confirmation envelopes on ada_plan/ada_preflight/ada_start_check.
"""
import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="mcp package (or tests dep stub) required")

ADA_MCP = Path(__file__).resolve().parents[1] / "opencode" / "ada_mcp.py"
_AUDIT = tempfile.mkdtemp(prefix="ada-mcp-gate-audit-")


def _load():
    os.environ["ADA_CONTROL_AUDIT_DIR"] = _AUDIT
    spec = importlib.util.spec_from_file_location(
        "ada_mcp_gate_under_test", ADA_MCP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def ada_mcp():
    mod = _load()
    mod._snapshot_cache.clear()
    mod.PENDING_NEXT_SELECTIONS.clear()
    yield mod
    mod.PENDING_NEXT_SELECTIONS.clear()


def _tool(mod, name):
    return mod.mcp._tool_manager._tools[name].fn


def _card(card_id, ref, title, column="backlog", priority="high",
          updated="2026-11-10 08:00", **extra):
    card = {
        "id": card_id, "ref": ref, "title": title, "column": column,
        "priority": priority, "updated": updated,
        "brief": f"{title} — self-contained work for the bounded lane",
        "spec": "verify with tests",
        "action": {}, "requests": [], "comms": [],
    }
    card.update(extra)
    return card


# --------------------------------------------------------------------------
# _selection_gate states
# --------------------------------------------------------------------------

def test_gate_open_when_no_pending_menu(ada_mcp):
    assert ada_mcp._selection_gate("ADA-0001", "") is None
    assert ada_mcp._selection_gate("ADA-0001", "any") is None


def test_gate_requires_issued_token_for_pending_card(ada_mcp):
    ada_mcp.PENDING_NEXT_SELECTIONS["ada-0001"] = ("tok-1", time.monotonic() + 900)
    assert ada_mcp._selection_gate("ADA-0001", "") == "selection_token_required"
    assert ada_mcp._selection_gate("ADA-0001", "wrong") == "selection_token_required"
    # correct token opens and consumes the entry
    assert ada_mcp._selection_gate("ADA-0001", "tok-1") is None
    assert ada_mcp.PENDING_NEXT_SELECTIONS == {}


def test_gate_rejects_card_outside_current_menu(ada_mcp):
    ada_mcp.PENDING_NEXT_SELECTIONS["ada-0001"] = ("tok-1", time.monotonic() + 900)
    assert ada_mcp._selection_gate("ADA-9999", "tok-1") == \
        "selection_not_in_current_menu"


def test_gate_prunes_expired_menu_entries(ada_mcp):
    ada_mcp.PENDING_NEXT_SELECTIONS["ada-0001"] = ("tok-1", time.monotonic() - 1)
    assert ada_mcp._selection_gate("ADA-9999", "") is None
    assert ada_mcp.PENDING_NEXT_SELECTIONS == {}


# --------------------------------------------------------------------------
# menu issuers (ada_next_card / ada_card_pick)
# --------------------------------------------------------------------------

def test_next_card_issues_selection_tokens(ada_mcp, monkeypatch):
    monkeypatch.setattr(ada_mcp, "_backlog_cards", lambda: [
        _card("c1", "ADA-0101", "write docs for the bridge"),
        _card("c2", "ADA-0102", "deploy the gpu render pipeline"),
    ])
    doc = json.loads(_tool(ada_mcp, "ada_next_card")())
    assert doc["ok"] is True
    assert doc["mutations_performed"] == []
    assert doc["candidates"], "expected at least one ranked candidate"
    tokens = {c.get("ref"): c.get("selection_token") for c in doc["candidates"]}
    assert all(tokens.values())
    # the risky deploy card is penalised below the safe docs card
    assert doc["candidates"][0]["ref"] == "ADA-0101"
    # issued tokens are bound in the pending menu
    for cand in doc["candidates"]:
        ref = str(cand["ref"]).lower()
        assert ref in ada_mcp.PENDING_NEXT_SELECTIONS
        assert ada_mcp.PENDING_NEXT_SELECTIONS[ref][0] == cand["selection_token"]


def test_card_pick_validates_strategy_and_mints_tokens(ada_mcp, monkeypatch):
    monkeypatch.setattr(ada_mcp, "_board_cards", lambda: [
        _card("c1", "ADA-0101", "stale doing", column="doing",
              updated="2026-10-01 08:00"),
        _card("c2", "ADA-0102", "done card", column="done"),
        _card("c3", "ADA-0103", "open request",
              requests=[{"ask": "?", "status": "open"}]),
    ])
    bad = json.loads(_tool(ada_mcp, "ada_card_pick")(strategy="bogus"))
    assert bad["ok"] is False
    assert "strategy must be one of" in bad["error"]

    doc = json.loads(_tool(ada_mcp, "ada_card_pick")(strategy="sweep", limit=5))
    assert doc["ok"] is True
    assert doc["mutations_performed"] == []
    ids = [p["card"]["id"] for p in doc["picks"]]
    assert ids == ["c1"]  # done + open-request cards are not actionable
    token = doc["picks"][0]["selection_token"]
    assert ada_mcp.PENDING_NEXT_SELECTIONS["ada-0101"][0] == token
    # picks are projected through _safe_card (bounded field set)
    assert "spec" in doc["picks"][0]["card"]
    assert "comms_tail" in doc["picks"][0]["card"]


def test_card_pick_marks_manual_queue_items_human_only(ada_mcp, monkeypatch):
    monkeypatch.setattr(ada_mcp, "_board_cards", lambda: [
        _card("manual", "ADA-0104", "human follow-up",
              action={"type": "manual", "status": "queued"}),
        _card("dispatch", "ADA-0105", "runnable task",
              action={"type": "dispatch", "status": "queued"}),
    ])

    doc = json.loads(_tool(ada_mcp, "ada_card_pick")(
        strategy="queue", limit=5))

    assert doc["ok"] is True
    assert doc["mutations_performed"] == []
    picks = {pick["card"]["id"]: pick for pick in doc["picks"]}
    assert picks["manual"]["why"] == \
        "queued for manual handling; not runner-dispatchable"
    assert picks["dispatch"]["why"] == "queued for dispatch"
    # Manual work remains selectable for Ada's read-only planning gates.
    token = picks["manual"]["selection_token"]
    assert token
    assert ada_mcp.PENDING_NEXT_SELECTIONS["ada-0104"][0] == token


# --------------------------------------------------------------------------
# gated tools: confirmation + token envelopes
# --------------------------------------------------------------------------

def test_plan_requires_exact_confirmation(ada_mcp):
    doc = json.loads(_tool(ada_mcp, "ada_plan")(
        "ADA-0101", selection_confirmation="ada-9999"))
    assert doc["ok"] is False
    assert doc["error"] == "selection_confirmation_required"
    assert doc["expected"] == "ADA-0101"


def test_plan_blocks_card_outside_issued_menu(ada_mcp, monkeypatch):
    monkeypatch.setattr(ada_mcp, "_backlog_cards", lambda: [
        _card("c1", "ADA-0101", "safe docs")])
    _tool(ada_mcp, "ada_next_card")()  # issues menu for ada-0101 only
    doc = json.loads(_tool(ada_mcp, "ada_plan")(
        "ADA-0102", selection_confirmation="ADA-0102",
        selection_token="whatever"))
    assert doc["ok"] is False
    assert doc["error"] == "selection_not_in_current_menu"


def test_plan_accepts_issued_token_and_consumes_it(ada_mcp, monkeypatch):
    monkeypatch.setattr(ada_mcp, "_backlog_cards", lambda: [
        _card("c1", "ADA-0101", "safe docs")])
    menu = json.loads(_tool(ada_mcp, "ada_next_card")())
    token = menu["candidates"][0]["selection_token"]
    monkeypatch.setattr(ada_mcp, "find_card", lambda ref: _card("c1", "ADA-0101", "safe docs"))
    doc = json.loads(_tool(ada_mcp, "ada_plan")(
        "ADA-0101", selection_confirmation="ADA-0101",
        selection_token=token))
    assert doc["ok"] is True
    assert doc["operation"] == "plan"
    assert ada_mcp.PENDING_NEXT_SELECTIONS == {}


def test_preflight_receipt_gates_and_dispatch_fields(ada_mcp, monkeypatch):
    monkeypatch.setattr(ada_mcp, "find_card", lambda ref: _card(
        "c1", "ADA-0101", "safe docs"))
    doc = json.loads(_tool(ada_mcp, "ada_preflight")(
        "ADA-0101", selection_confirmation="ADA-0101"))
    assert doc["ok"] is True
    assert doc["operation"] == "preflight"
    assert doc["workflow_id"].startswith("ada-preflight-")
    gates = doc["start_check"]["gates"]
    assert gates == {
        "card_found": True,
        "backlog": True,
        "no_open_requests": True,
        "safe_scope": True,
        "typed_dispatch_available": False,
    }
    assert doc["dispatch_check"]["required_fields"] == [
        "type", "repo", "executor", "model", "profile",
        "target_host", "artifact", "verifier"]
    assert doc["dispatch_check"]["missing_fields"]
    assert doc["dispatch_check"]["ok"] is False
    assert doc["ready_for_dispatch"] is False
    assert doc["mutations_performed"] == []
    assert doc["requires_native_followup"] is True


def test_preflight_flags_risk_terms_in_safe_scope_gate(ada_mcp, monkeypatch):
    risky = _card("c1", "ADA-0101", "deploy to production and rotate credential")
    monkeypatch.setattr(ada_mcp, "find_card", lambda ref: risky)
    doc = json.loads(_tool(ada_mcp, "ada_preflight")(
        "ADA-0101", selection_confirmation="ADA-0101"))
    assert doc["start_check"]["gates"]["safe_scope"] is False
    assert "deploy" in doc["start_check"]["risk_terms"]
    assert doc["ready_for_dispatch"] is False


def test_status_clears_pending_menu(ada_mcp, monkeypatch):
    ada_mcp.PENDING_NEXT_SELECTIONS["ada-0101"] = ("t", time.monotonic() + 900)
    monkeypatch.setattr(ada_mcp, "find_card", lambda ref: _card("c1", "ADA-0101", "x"))
    doc = json.loads(_tool(ada_mcp, "ada_status")("ADA-0101"))
    assert doc["ok"] is True
    assert ada_mcp.PENDING_NEXT_SELECTIONS == {}


def test_start_check_requires_handoff_confirmation(ada_mcp):
    doc = json.loads(_tool(ada_mcp, "ada_start_check")(
        "ADA-0101", handoff_confirmation="other"))
    assert doc["ok"] is False
    assert doc["error"] == "handoff_confirmation_required"


def test_dispatch_check_reports_missing_envelope_fields(ada_mcp, monkeypatch):
    monkeypatch.setattr(ada_mcp, "find_card", lambda ref: _card("c1", "ADA-0101", "x"))
    doc = json.loads(_tool(ada_mcp, "ada_dispatch_check")("ADA-0101"))
    assert doc["ok"] is False
    assert set(doc["missing_fields"]) == {
        "type", "repo", "executor", "model", "profile",
        "target_host", "artifact", "verifier"}
    assert doc["typed_dispatch_available"] is False
    assert doc["mutations_performed"] == []


def test_dispatch_check_resolves_readonly_profile_verifier(ada_mcp, monkeypatch):
    actioned = _card("c1", "ADA-0101", "x", action={
        "type": "dispatch", "repo": "chaba", "executor": "opencode",
        "model": "litellm/gemini-2.5-flash", "profile": "ops-readonly",
        "target_host": "idc02", "artifact": "out.json"})
    monkeypatch.setattr(ada_mcp, "find_card", lambda ref: actioned)
    doc = json.loads(_tool(ada_mcp, "ada_dispatch_check")("ADA-0101"))
    assert doc["ok"] is True          # profile resolves the verifier
    assert doc["missing_fields"] == []


def test_positive_risk_terms_ignores_negations(ada_mcp):
    assert ada_mcp._positive_risk_terms(
        "do not deploy this; never touch production") == []
    assert "deploy" in ada_mcp._positive_risk_terms("please deploy the service")
