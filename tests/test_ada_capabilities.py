"""ada_capabilities tools.d adapter — fixture snapshot, no network
(card ada-capability-verb-wiring; chaba docs/kb/ada-tools-verb-contract.md)."""
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

MOD = Path(__file__).resolve().parents[1] / "backend" / "tools.d" / "ada_capabilities.py"

SNAPSHOT = {
    "schema": 1,
    "generated_at": "2026-11-24T00:00:00+00:00",
    "source": {"repo": "ada-voice", "head": "abc1234",
               "head_date": "2026-11-24", "behind": "0"},
    "counts": {"builtin": 2, "dropin": 1, "guest": 0,
               "aliases": 1, "families": 1},
    "tools": [
        {"name": "ada_remember", "layer": "builtin",
         "purpose": "Store or update a memory.", "policy": "read",
         "triggers": ["remember that"], "family": "memory_remember",
         "source_file": "backend/realtime_provider.py"},
        {"name": "kanban", "layer": "dropin",
         "purpose": "Kanban board ops.", "policy": "read",
         "triggers": [], "family": "kanban",
         "source_file": "backend/tools.d/kanban.py"},
        {"name": "web_search", "layer": "builtin",
         "purpose": "Search the public internet.", "policy": "read",
         "triggers": [], "family": "",
         "source_file": "backend/realtime_provider.py"},
    ],
    "aliases": {"tasks_add": "tasks", "old_board_write": "kanban"},
    "families": {
        "kanban": {"canonical": "kanban", "absorbed": ["old_board_write"],
                   "status": "merged", "merge_card": "ada-kanban-access",
                   "scenario": "kanban_review"},
        "memory_remember": {"canonical": "ada_remember",
                            "absorbed": ["vocab_note"],
                            "status": "merged"},
    },
}


def _load():
    spec = importlib.util.spec_from_file_location("ada_capabilities_tool", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def snap(tmp_path, monkeypatch):
    p = tmp_path / "ada-tools-snapshot.json"
    p.write_text(json.dumps(SNAPSHOT))
    monkeypatch.setenv("ADA_CAPABILITIES_SNAPSHOT", str(p))
    return p


def test_declaration_is_slim_and_read_policy():
    m = _load()
    assert m.DECLARATION["name"] == "ada_capabilities"
    assert len(m.DECLARATION["description"]) <= 300
    assert "topic" in m.DECLARATION["parameters"]["properties"]
    assert "required" not in m.DECLARATION["parameters"]


def test_missing_snapshot_is_ok_false(snap, tmp_path, monkeypatch):
    m = _load()
    monkeypatch.setenv("ADA_CAPABILITIES_SNAPSHOT",
                       str(tmp_path / "nope.json"))
    m._cache.update(key=None, data=None, error=None)
    out = asyncio.run(m.run(None))
    assert out["ok"] is False
    assert "not generated" in out["error"]


def test_summary_groups_and_counts(snap):
    m = _load()
    out = asyncio.run(m.run(None))
    assert out["ok"] is True
    assert out["capability_count"] == 3
    assert "2 core + 1 add-on" in out["answer"]
    assert "as of 2026-11-24" in out["answer"]
    assert "memory & voice" in out["answer"]
    assert "kanban, jobs & hosts" in out["answer"]
    assert "other tools" in out["answer"]  # web_search
    assert "Ask me about a specific tool" in out["answer"]
    # absorbed alias names are never presented as capabilities
    assert "old_board_write" not in out["answer"]
    assert "tasks_add" not in out["answer"]


def test_detail_exact_tool(snap):
    m = _load()
    out = asyncio.run(m.run(None, topic="kanban"))
    assert out["ok"] is True
    assert "Kanban board ops." in out["answer"]
    assert out["tool"]["name"] == "kanban"


def test_detail_retired_name_is_honest(snap):
    m = _load()
    out = asyncio.run(m.run(None, topic="old_board_write"))
    assert out["ok"] is True
    assert "retired" in out["answer"]
    assert "merged into kanban" in out["answer"]
    assert out["merged_into"] == "kanban"


def test_detail_family(snap):
    m = _load()
    out = asyncio.run(m.run(None, topic="memory_remember"))
    assert out["ok"] is True
    assert out["family"]["canonical"] == "ada_remember"
    assert "ada_remember" in out["answer"]
    assert "vocab_note" in out["answer"]  # absorbed listed as retired


def test_detail_keyword_and_miss(snap):
    m = _load()
    hit = asyncio.run(m.run(None, topic="internet"))
    assert "web_search" in hit["answer"]
    miss = asyncio.run(m.run(None, topic="teleport"))
    assert miss["ok"] is True
    assert "Nothing" in miss["answer"]


def test_schema_major_bump_refuses(snap, monkeypatch):
    m = _load()
    p2 = snap.parent / "snap2.json"
    p2.write_text(json.dumps(dict(SNAPSHOT, schema=2)))
    monkeypatch.setenv("ADA_CAPABILITIES_SNAPSHOT", str(p2))
    out = asyncio.run(m.run(None))
    assert out["ok"] is False
    assert "schema" in out["error"]
