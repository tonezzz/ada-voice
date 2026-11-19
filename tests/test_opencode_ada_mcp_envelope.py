"""Unit tests for opencode/ada_mcp.py staged-envelope tools
(ada_envelope_draft / ada_envelope_stage) and the dispatch liveness
guards on ada_dispatch_check / ada_card_action."""
import importlib.util
import json
import sys
import types
from pathlib import Path


def _load_ada_mcp():
    mcp_mod = types.ModuleType("mcp")
    server_mod = types.ModuleType("mcp.server")
    fastmcp_mod = types.ModuleType("mcp.server.fastmcp")

    class FastMCP:
        def __init__(self, *args, **kwargs):
            pass

        def tool(self, *args, **kwargs):
            def decorator(fn):
                return fn
            return decorator

    fastmcp_mod.FastMCP = FastMCP
    sys.modules.setdefault("mcp", mcp_mod)
    sys.modules.setdefault("mcp.server", server_mod)
    sys.modules.setdefault("mcp.server.fastmcp", fastmcp_mod)

    path = Path(__file__).resolve().parents[1] / "opencode" / "ada_mcp.py"
    spec = importlib.util.spec_from_file_location(
        "ada_mcp_envelope_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ada_mcp = _load_ada_mcp()


def _card(action=None, status=None):
    a = {"type": "dispatch", "repo": "chaba",
         "lane": "free_lane",
         "model": "litellm/gemini-3.5-flash-lite"}
    if action:
        a.update(action)
    if status:
        a["status"] = status
    return {"id": "ada-test-card", "ref": "ADA-9999",
            "title": "test card", "action": a, "claim": {}}


def _install_card(monkeypatch, card):
    monkeypatch.setattr(ada_mcp, "find_card", lambda ref: card)
    return card


GOOD_ACTION = {
    "executor": "opencode",
    "agent_profile": "ops-readonly",
    "target_host": "idc02",
    "artifact": "evals/opencode-lane/out/ada-test-card.json",
    "repo": "chaba",
}


def test_draft_builds_complete_envelope(monkeypatch):
    card = _install_card(monkeypatch, _card())
    out = json.loads(ada_mcp.ada_envelope_draft("ADA-9999", GOOD_ACTION))
    assert out["ok"] is True, out
    env = out["action"]["lane_envelope"]
    for k in ("id", "card", "task_class", "stage", "target_host",
              "executor", "model", "provider", "mcp_allowlist",
              "allowed_paths", "artifact", "verifier", "escalation",
              "rollback", "heartbeat"):
        assert k in env, k
    assert env["stage"] == "S3"
    assert env["target_host"] == "idc02"
    assert env["executor"] == "opencode"
    assert env["profile"] == "ops-readonly"
    assert env["mcp_allowlist"] == []
    assert env["artifact"]["path"] == GOOD_ACTION["artifact"]
    assert env["verifier"]["type"] == "held_out_py"
    assert env["artifact"]["path"].startswith(env["allowed_paths"][0])
    assert out["missing_fields"] == []
    # draft never mutates
    assert "lane_envelope" not in card["action"]


def test_draft_refuses_unadmitted_profile(monkeypatch):
    _install_card(monkeypatch, _card())
    bad = dict(GOOD_ACTION, agent_profile="doc-draft")
    out = json.loads(ada_mcp.ada_envelope_draft("ADA-9999", bad))
    assert out["ok"] is False
    assert any("not admitted" in p for p in out["problems"])
    assert "lane_envelope" not in out["action"]


def test_draft_rejects_unsafe_artifact(monkeypatch):
    _install_card(monkeypatch, _card())
    bad = dict(GOOD_ACTION, artifact="../etc/passwd")
    out = json.loads(ada_mcp.ada_envelope_draft("ADA-9999", bad))
    assert out["ok"] is False
    assert any("artifact" in p for p in out["problems"])


def test_draft_requires_executor_without_lane_hint(monkeypatch):
    card = _card({"lane": ""})
    _install_card(monkeypatch, card)
    out = json.loads(ada_mcp.ada_envelope_draft(
        "ADA-9999", {"repo": "chaba",
                     "artifact": "evals/opencode-lane/out/x.json"}))
    assert out["ok"] is False
    assert "executor" in out["missing_fields"] or any(
        "executor" in p for p in out["problems"])


def test_stage_preview_does_not_post(monkeypatch):
    _install_card(monkeypatch, _card())
    posted = []
    monkeypatch.setattr(ada_mcp, "_post_json",
                        lambda *a, **k: posted.append(a) or {})
    out = json.loads(ada_mcp.ada_envelope_stage("ADA-9999", GOOD_ACTION))
    assert out["preview"] is True
    assert posted == []
    assert out["ok"] is True


def test_stage_confirmed_posts_card_and_verifies(monkeypatch):
    card = _card()
    _install_card(monkeypatch, card)
    posted = []

    def fake_post(path, payload, timeout=3):
        posted.append((path, payload))
        # simulate board-api merge + queue
        card["action"].update(payload["action"])
        card["action"]["status"] = "queued"
        return {"ok": True, "message": "card ada-test-card re-queued"}

    monkeypatch.setattr(ada_mcp, "_post_json", fake_post)
    out = json.loads(ada_mcp.ada_envelope_stage(
        "ADA-9999", GOOD_ACTION, confirmed=True))
    assert out["ok"] is True, out
    assert posted and posted[0][0] == "/card"
    body = posted[0][1]
    assert body["on_exists"] == "queue"
    assert body["action"]["lane_envelope"]["stage"] == "S3"
    assert out["dispatch_check"]["ok"] is True
    assert out["action_status"] == "queued"


def test_stage_refuses_while_running(monkeypatch):
    _install_card(monkeypatch, _card(status="running"))
    posted = []
    monkeypatch.setattr(ada_mcp, "_post_json",
                        lambda *a, **k: posted.append(a) or {})
    out = json.loads(ada_mcp.ada_envelope_stage(
        "ADA-9999", GOOD_ACTION, confirmed=True))
    assert out["ok"] is False
    assert out["blocked_reason"] == "active_attempt"
    assert posted == []


def test_dispatch_check_reports_live_block(monkeypatch):
    _install_card(monkeypatch, _card(status="running"))
    out = json.loads(ada_mcp.ada_dispatch_check("ADA-9999"))
    assert out["blocked"] is True
    assert out["action_status"] == "running"
    assert "executor" in out["missing_fields"]
    assert out["envelope_tool"] == "ada_envelope_draft"


def test_card_action_queue_refused_while_running(monkeypatch):
    _install_card(monkeypatch, _card(status="running"))
    posted = []
    monkeypatch.setattr(ada_mcp, "_post_json",
                        lambda *a, **k: posted.append(a) or {})
    out = json.loads(ada_mcp.ada_card_action(
        "ADA-9999", "queue", confirmed=True))
    assert out["ok"] is False
    assert out["blocked_reason"] == "active_attempt"
    assert posted == []


def test_card_action_hold_still_previews_when_running(monkeypatch):
    _install_card(monkeypatch, _card(status="running"))
    out = json.loads(ada_mcp.ada_card_action("ADA-9999", "hold"))
    assert out["preview"]["action_status"] == "running"
