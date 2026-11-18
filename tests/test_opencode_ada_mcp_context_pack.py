"""Unit tests for opencode/ada_mcp.py context retrieval features.

Covers ada_context_pack (composition, budgets, provenance, read-only
contract), the bounded excerpt/selector machinery behind ada_ssot_get,
and the _context_pack markdown work pack.
"""
import importlib.util
import json
import os
import tempfile
import time
import types
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="mcp package (or tests dep stub) required")

ADA_MCP = Path(__file__).resolve().parents[1] / "opencode" / "ada_mcp.py"
_AUDIT = tempfile.mkdtemp(prefix="ada-mcp-ctx-audit-")


def _load():
    os.environ["ADA_CONTROL_AUDIT_DIR"] = _AUDIT
    spec = importlib.util.spec_from_file_location(
        "ada_mcp_context_under_test", ADA_MCP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def ada_mcp():
    mod = _load()
    mod._snapshot_cache.clear()
    mod.PENDING_NEXT_SELECTIONS.clear()
    return mod


def _tool(mod, name):
    return mod.mcp._tool_manager._tools[name].fn


def _card(card_id="c1", ref="ADA-0172", title="fast board reads", **extra):
    card = {
        "id": card_id, "ref": ref, "title": title,
        "column": "review", "priority": "high",
        "updated": "2026-11-18 08:00",
        "brief": "compact and projected snapshots beat the 9MB dump",
        "spec": "see opencode/ada_mcp.py for the bridge implementation",
        "action": {}, "requests": [],
        "comms": [{"at": "2026-11-18", "from": "devin", "text": "shipped"}],
    }
    card.update(extra)
    return card


# --------------------------------------------------------------------------
# ada_context_pack composition
# --------------------------------------------------------------------------

def test_context_pack_composes_sources_readonly(ada_mcp, monkeypatch):
    monkeypatch.setattr(
        ada_mcp, "_ssot_search",
        lambda query, deadline, limit=3: ([{"key": "k1", "source": "ssot"}], []))
    monkeypatch.setattr(
        ada_mcp, "recall_memory",
        lambda query, deadline=None: {"lessons": [{"slug": "l1"}],
                                      "devin_sessions": [], "errors": []})
    monkeypatch.setattr(ada_mcp, "_context_card",
                        lambda ref, deadline: _card())
    raw = _tool(ada_mcp, "ada_context_pack")(
        query="board snapshots", card_reference="ADA-0172")
    doc = json.loads(raw)
    assert doc["ok"] is True
    assert doc["operation"] == "context-pack"
    assert doc["mutations_performed"] == []
    assert doc["secrets_read"] is False
    assert doc["profile"] == "ssot-research"
    assert doc["ssot"] == [{"key": "k1", "source": "ssot"}]
    assert doc["memory"]["lessons"] == [{"slug": "l1"}]
    assert doc["card"]["ref"] == "ADA-0172"
    assert "board-api:/card" in doc["allowed_sources"]
    assert doc["budgets"]["deadline_s"] == 8.0
    assert doc["errors"] == []
    assert doc["bridge"]["version"] == ada_mcp.BRIDGE_VERSION


def test_context_pack_profile_fallback_and_limit_bound(ada_mcp, monkeypatch):
    monkeypatch.setattr(ada_mcp, "_ssot_search", lambda *a, **k: ([], []))
    monkeypatch.setattr(ada_mcp, "recall_memory",
                        lambda *a, **k: {"errors": []})
    doc = json.loads(_tool(ada_mcp, "ada_context_pack")(
        query="q", profile="not-a-profile", limit=99))
    assert doc["profile"] == "ssot-research"
    assert doc["budgets"]["max_results_per_source"] == 5


def test_context_pack_honours_include_flags(ada_mcp, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("source should not be queried")
    monkeypatch.setattr(ada_mcp, "_ssot_search", boom)
    monkeypatch.setattr(ada_mcp, "recall_memory", boom)
    doc = json.loads(_tool(ada_mcp, "ada_context_pack")(
        query="q", include_ssot=False, include_memory=False))
    assert doc["ok"] is True
    assert doc["ssot"] == []
    assert doc["memory"] is None


def test_context_pack_bad_card_reference_degrades_to_error(ada_mcp, monkeypatch):
    monkeypatch.setattr(ada_mcp, "_ssot_search", lambda *a, **k: ([], []))
    monkeypatch.setattr(ada_mcp, "recall_memory",
                        lambda *a, **k: {"errors": []})

    def missing(ref, deadline):
        raise LookupError(f"no board card matched {ref!r}")
    monkeypatch.setattr(ada_mcp, "_context_card", missing)
    doc = json.loads(_tool(ada_mcp, "ada_context_pack")(
        query="q", card_reference="NOPE-0000"))
    assert doc["ok"] is True               # pack still returns, card degraded
    assert doc["card"] is None
    assert any(e.startswith("card:") for e in doc["errors"])


def test_context_pack_source_failure_lands_in_errors(ada_mcp, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("mddb down")
    monkeypatch.setattr(ada_mcp, "_ssot_search", boom)
    monkeypatch.setattr(ada_mcp, "recall_memory", boom)
    doc = json.loads(_tool(ada_mcp, "ada_context_pack")(query="q"))
    assert doc["ok"] is True
    assert any(e.startswith("ssot:") for e in doc["errors"])
    assert any(e.startswith("memory:") for e in doc["errors"])


def test_context_pack_deadline_marks_unfinished_source(ada_mcp, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ada_mcp, "time", types.SimpleNamespace(
        monotonic=lambda: clock[0], time=time.time, strftime=time.strftime))

    def card_then_blow_deadline(ref, deadline):
        clock[0] += 30.0  # burn the 8s pack budget during card resolution
        return _card()
    monkeypatch.setattr(ada_mcp, "_context_card", card_then_blow_deadline)

    def slow(query, deadline):
        time.sleep(0.3)  # real sleep: still running after the 0.1s wait
        return {"errors": []}
    monkeypatch.setattr(ada_mcp, "_ssot_search",
                        lambda q, d, limit=3: (time.sleep(0.3), ([], []))[1])
    monkeypatch.setattr(ada_mcp, "recall_memory", slow)
    doc = json.loads(_tool(ada_mcp, "ada_context_pack")(
        query="q", card_reference="ADA-0172"))
    assert doc["ok"] is True
    assert "ssot:deadline" in doc["errors"]
    assert "memory:deadline" in doc["errors"]


def test_card_context_builds_work_pack(ada_mcp, monkeypatch, tmp_path):
    monkeypatch.setattr(ada_mcp, "find_card", lambda ref: _card())
    (tmp_path / "opencode").mkdir()
    (tmp_path / "opencode" / "ada_mcp.py").write_text("# bridge\n")
    monkeypatch.setattr(ada_mcp, "WORKSPACE", tmp_path)
    doc = json.loads(_tool(ada_mcp, "ada_card_context")("ADA-0172"))
    assert doc["ok"] is True
    assert doc["mutations_performed"] == []
    pack = doc["pack_markdown"]
    assert "# Card ADA-0172" in pack
    assert "## Spec (verbatim)" in pack
    assert "## Gates & conventions" in pack
    assert "Done-condition" in pack
    assert "opencode/ada_mcp.py" in pack   # referenced file was found+excerpted


# --------------------------------------------------------------------------
# _context_pack markdown + text extraction
# --------------------------------------------------------------------------

def test_context_card_text_handles_string_rows(ada_mcp):
    card = _card(comms=["a string comm", {"at": "t", "from": "x", "text": "d"}],
                 requests=["a string request", {"ask": "dict ask"}])
    text = ada_mcp._context_card_text(card)
    assert "a string comm" in text
    assert "a string request" in text
    assert "dict ask" in text


def test_context_pack_lists_open_requests(ada_mcp):
    card = _card(requests=[
        {"ask": "which lane?", "options": ["a", "b"], "status": "open"},
        {"ask": "answered", "status": "answered"},
    ])
    pack = ada_mcp._context_pack(card, Path("/nonexistent"))
    assert "## Open requests" in pack
    assert "which lane?" in pack and "a, b" in pack
    assert "- answered (" not in pack


def test_done_condition_prefers_verification_sentence(ada_mcp):
    card = _card(spec="Build the thing. It is nice. Verify with pytest tests/.")
    assert "pytest" in ada_mcp._context_done_condition(card)
    card2 = _card(spec="no verbs here", brief="short brief")
    assert ada_mcp._context_done_condition(card2) == "short brief"
    card3 = _card(spec="", brief="")
    assert "no explicit done-condition" in ada_mcp._context_done_condition(card3)


# --------------------------------------------------------------------------
# excerpt windows + selector blocks (ssot_get machinery)
# --------------------------------------------------------------------------

def test_excerpt_window_short_circuits_small_content(ada_mcp):
    assert ada_mcp._excerpt_window("tiny", "q") == "tiny"


def test_excerpt_window_centres_on_best_match(ada_mcp):
    lines = [f"filler line {i}" for i in range(50)]
    lines[30] = "the tony-macbook spec lives here with gpu details"
    content = "\n".join(lines)
    excerpt = ada_mcp._excerpt_window(content, "tony-macbook gpu", width=200)
    assert "tony-macbook" in excerpt
    assert excerpt.startswith("\u2026") or excerpt.endswith("\u2026")
    assert len(excerpt) <= 210


def test_excerpt_window_no_match_falls_back_to_head(ada_mcp):
    content = "head\n" + "x" * 5000
    assert ada_mcp._excerpt_window(content, "zzz") == content[:1200]


def test_selector_variants_tony_prefix_symmetry(ada_mcp):
    assert ada_mcp._selector_variants("tony-macbook") == {"tony-macbook", "macbook"}
    assert ada_mcp._selector_variants("macbook") == {"macbook", "tony-macbook"}
    assert ada_mcp._selector_variants("") == set()


def test_selector_block_matches_yaml_key(ada_mcp):
    content = "hosts:\n  tony-dell:\n    cpu: r5\n  tony-macbook:\n    cpu: m3\n    ram: 24g\nother:\n"
    block = ada_mcp._selector_block(content, "tony-macbook", 12000)
    assert block["matched"] is True
    assert "cpu: m3" in block["text"]
    assert "tony-dell" not in block["text"]
    assert block["line"] == 4


def test_selector_block_unmatched_excerpts_around_term(ada_mcp):
    content = "intro\n" + "pad\n" * 200 + "needle mention\n" + "pad\n" * 200
    block = ada_mcp._selector_block(content, "unrelatedselector", 400)
    assert block["matched"] is False
    assert len(block["text"]) <= 400


def test_ssot_key_candidates_normalises_spelling(ada_mcp):
    c = ada_mcp._ssot_key_candidates("infrastructure-ssot", "ssot.host-capacity")
    assert c[0] == "ssot.host-capacity"
    assert "infrastructure-ssot.ssot.host-capacity" in c
    assert "host-capacity" in c
    assert "infrastructure-ssot.host-capacity" in c
    assert ada_mcp._ssot_key_candidates("infrastructure-ssot", "") == []


def test_related_pointers_excludes_self(ada_mcp):
    content = ("see docs/ssot/ssot.hosts.yml and docs/ssot/ssot.jobs.yml "
               "plus ssot.hosts.yml again")
    found = ada_mcp._related_pointers(
        content, exclude=("docs/ssot/ssot.hosts.yml", "ssot.hosts.yml"))
    # the bare ssot.* pattern also matches the tail of a docs/ssot/ path —
    # dedup is by exact string, so both spellings of the other doc remain.
    assert found == ["docs/ssot/ssot.jobs.yml", "ssot.jobs.yml"]


# --------------------------------------------------------------------------
# ada_ssot_get tool envelope (mocked transport)
# --------------------------------------------------------------------------

def test_ssot_get_envelope_and_allowlist(ada_mcp, monkeypatch):
    doc_payload = {
        "key": "infrastructure-ssot.host-capacity",
        "contentMd": "hosts:\n  tony-macbook:\n    cpu: m3\n",
        "meta": {"original_path": ["docs/ssot/infrastructure/ssot.host-capacity.yml"],
                 "title": ["host capacity"]},
    }
    import urllib.error

    def fake_mddb(path, payload, timeout=5):
        if payload.get("key") == "infrastructure-ssot.host-capacity":
            return doc_payload
        raise urllib.error.HTTPError(
            url="x", code=404, msg="nf", hdrs=None, fp=None)
    monkeypatch.setattr(ada_mcp, "_mddb_post", fake_mddb)
    raw = _tool(ada_mcp, "ada_ssot_get")(
        collection="infrastructure-ssot", key="host-capacity",
        selector="tony-macbook")
    doc = json.loads(raw)
    assert doc["ok"] is True
    assert doc["mutations_performed"] == []
    assert doc["key"] == "infrastructure-ssot.host-capacity"
    assert doc["matched_selector"] is True
    assert "cpu: m3" in doc["content"]
    assert doc["secrets_read"] is False

    denied = json.loads(_tool(ada_mcp, "ada_ssot_get")(
        collection="household-bank", key="anything"))
    assert denied["ok"] is False
    assert "not allowlisted" in denied["error"]
    assert denied["mutations_performed"] == []


def test_ssot_get_missing_key_is_ok_false(ada_mcp, monkeypatch):
    import urllib.error
    def not_found(path, payload, timeout=5):
        raise urllib.error.HTTPError(
            url="x", code=404, msg="nf", hdrs=None, fp=None)
    monkeypatch.setattr(ada_mcp, "_mddb_post", not_found)
    doc = json.loads(_tool(ada_mcp, "ada_ssot_get")(
        collection="infrastructure-ssot", key="does-not-exist"))
    assert doc["ok"] is False
    assert "not found" in doc["error"]
    assert doc["mutations_performed"] == []
