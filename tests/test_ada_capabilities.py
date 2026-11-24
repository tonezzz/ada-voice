"""ada_capabilities tools.d — snapshot-driven "what can you do" read path
(card ada-capabilities-verb-wiring; contract chaba
docs/kb/ada-tools-verb-contract.md). No network; a fixture snapshot stands
in for the deploy-shipped backend/data/ada-tools-snapshot.json."""
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

MOD = (Path(__file__).resolve().parents[1]
       / "backend" / "tools.d" / "ada_capabilities.py")
REAL_SNAPSHOT = (Path(__file__).resolve().parents[1]
                 / "backend" / "data" / "ada-tools-snapshot.json")


def _load():
    spec = importlib.util.spec_from_file_location("ada_capabilities_tool", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _tool(name, layer="dropin", purpose="", policy="read", family="",
          triggers=None, backend="", **extra):
    row = {
        "name": name, "layer": layer, "purpose": purpose,
        "description": purpose, "backend": backend, "registry": "",
        "policy": policy, "timeout_s": 10, "secondary": False,
        "schema": "", "guide": True, "triggers": triggers or [],
        "last_changed": "2026-11-20", "family": family,
        "source_file": f"backend/tools.d/{name}.py",
    }
    row.update(extra)
    return row


def _fixture(**over):
    snap = {
        "schema": 1,
        "generated_at": "2026-11-20T00:00:00+00:00",
        "source": {"repo": "ada-voice", "path": "/x", "head": "abc1234",
                   "head_date": "2026-11-20", "behind": "0"},
        "counts": {"builtin": 2, "dropin": 4, "guest": 1, "aliases": 2,
                   "families": 2, "count_cap": 115, "target_count": 38},
        "tools": [
            # builtin + guest rows for the same name — counts once, core.
            _tool("ada_memory_search", "builtin",
                  "Search memory banks.", family="memory_search",
                  triggers=["what do you remember about the gate code"]),
            _tool("ada_memory_search", "guest",
                  "Search public guest memories.", family="memory_search"),
            _tool("home_search", "builtin", "Find HA devices and sensors.",
                  policy="", family="ha_search",
                  backend="ha"),
            _tool("kanban", "dropin", "Kanban board ops.",
                  family="kanban", triggers=["what is on the board"]),
            _tool("wp", "dropin", "WordPress boards writer.",
                  family="wp"),
            # absorbed name still carrying a tools.d implementation seat —
            # must never appear as a capability.
            _tool("wp_post", "dropin", "Old WP poster."),
            # unmapped name -> misc group.
            _tool("mystery_new_tool", "dropin", "Unmapped tool.",
                  policy="confirmed"),
        ],
        "aliases": {"wp_post": "wp", "cctv_snapshot": "ada_camera_snapshot"},
        "families": {
            "memory_search": {"canonical": "ada_memory_search",
                              "absorbed": ["guest_recall"],
                              "status": "merged"},
            "kanban": {"canonical": "kanban", "absorbed": ["ada_board_write"],
                       "status": "merged"},
        },
    }
    snap.update(over)
    return snap


def _write(tmp_path, snap) -> Path:
    p = tmp_path / "ada-tools-snapshot.json"
    p.write_text(json.dumps(snap), encoding="utf-8")
    return p


def _env(monkeypatch, path):
    monkeypatch.setenv("ADA_TOOLS_SNAPSHOT", str(path))


def test_declaration_is_slim_and_topic_optional():
    m = _load()
    assert m.DECLARATION["name"] == "ada_capabilities"
    assert len(m.DECLARATION["description"]) <= 300
    assert "topic" in m.DECLARATION["parameters"]["properties"]
    assert "required" not in m.DECLARATION["parameters"]


def test_overview_grouped_answer(tmp_path, monkeypatch):
    m = _load()
    _env(monkeypatch, _write(tmp_path, _fixture()))
    out = asyncio.run(m.run(None))
    assert out["ok"] is True and out["mode"] == "overview"
    answer = out["answer"]
    # one-line count summary — wp_post is an alias key, never surface;
    # ada_memory_search counts once despite builtin+guest rows.
    assert "I have 5 capabilities — 2 core + 3 add-on tools — as of 2026-11-20." in answer
    # grouped headline list + the ask-about-a-tool offer
    for label in ("memory & recall", "home control & status",
                  "kanban, jobs & ops", "docs, CMS & knowledge", "misc"):
        assert label in answer
    assert "Ask me about a specific tool or area for details." in answer
    # canonical names only — absorbed names are never presented
    assert "wp_post" not in answer
    assert "cctv_snapshot" not in answer
    assert "ada_memory_search" in answer and "home_search" in answer
    # structured groups carry the full lists
    groups = {g["label"]: g["tools"] for g in out["groups"]}
    assert groups["misc"] == ["mystery_new_tool"]
    assert groups["memory & recall"] == ["ada_memory_search"]


def test_overview_is_deterministic(tmp_path, monkeypatch):
    m = _load()
    _env(monkeypatch, _write(tmp_path, _fixture()))
    a = asyncio.run(m.run(None))["answer"]
    b = asyncio.run(m.run(None))["answer"]
    assert a == b


def test_detail_by_tool_name(tmp_path, monkeypatch):
    m = _load()
    _env(monkeypatch, _write(tmp_path, _fixture()))
    out = asyncio.run(m.run(None, topic="kanban"))
    assert out["ok"] is True and out["kind"] == "tool"
    assert "Kanban board ops." in out["answer"]
    assert "policy read" in out["answer"]
    assert out["tool_info"]["name"] == "kanban"


def test_detail_by_family(tmp_path, monkeypatch):
    m = _load()
    _env(monkeypatch, _write(tmp_path, _fixture()))
    out = asyncio.run(m.run(None, topic="memory_search"))
    assert out["ok"] is True and out["kind"] == "family"
    assert "ada_memory_search" in out["answer"]
    assert out["family_info"]["canonical"] == "ada_memory_search"


def test_detail_by_area_label(tmp_path, monkeypatch):
    m = _load()
    _env(monkeypatch, _write(tmp_path, _fixture()))
    out = asyncio.run(m.run(None, topic="home control"))
    assert out["ok"] is True and out["kind"] == "group"
    assert "home_search" in out["answer"]


def test_detail_absorbed_name_redirects(tmp_path, monkeypatch):
    m = _load()
    _env(monkeypatch, _write(tmp_path, _fixture()))
    out = asyncio.run(m.run(None, topic="wp_post"))
    assert out["ok"] is True and out["kind"] == "tool"
    assert out["tool"] == "wp"
    assert "old name" in out["answer"]


def test_detail_unknown_topic_fails_honestly(tmp_path, monkeypatch):
    m = _load()
    _env(monkeypatch, _write(tmp_path, _fixture()))
    out = asyncio.run(m.run(None, topic="zzz-no-such-thing"))
    assert out["ok"] is False
    assert "no capability" in out["error"]


def test_missing_snapshot_never_fabricates(tmp_path, monkeypatch):
    m = _load()
    _env(monkeypatch, tmp_path / "does-not-exist.json")
    out = asyncio.run(m.run(None))
    assert out["ok"] is False
    assert "capability list not generated" in out["error"]


def test_corrupt_snapshot_fails(tmp_path, monkeypatch):
    m = _load()
    p = tmp_path / "snap.json"
    p.write_text("{not json", encoding="utf-8")
    _env(monkeypatch, p)
    out = asyncio.run(m.run(None))
    assert out["ok"] is False
    assert "capability list not generated" in out["error"]


def test_schema_bump_refuses_with_version_note(tmp_path, monkeypatch):
    m = _load()
    _env(monkeypatch, _write(tmp_path, _fixture(schema=2)))
    out = asyncio.run(m.run(None))
    assert out["ok"] is False
    assert "schema" in out["error"] and "2" in out["error"]


def test_stale_snapshot_still_answers_with_caveat(tmp_path, monkeypatch):
    m = _load()
    snap = _fixture()
    snap["source"]["behind"] = "9"
    _env(monkeypatch, _write(tmp_path, snap))
    out = asyncio.run(m.run(None))
    assert out["ok"] is True
    assert "9 commit(s) behind" in out["answer"]
    assert out["snapshot"]["stale"] is True
    assert out["snapshot"]["head_date"] == "2026-11-20"


def test_empty_canonical_surface_fails(tmp_path, monkeypatch):
    m = _load()
    snap = _fixture(tools=[], aliases={}, families={})
    _env(monkeypatch, _write(tmp_path, snap))
    out = asyncio.run(m.run(None))
    assert out["ok"] is False


@pytest.mark.skipif(not REAL_SNAPSHOT.is_file(),
                    reason="deploy snapshot not present in this checkout")
def test_shipped_snapshot_answers(tmp_path, monkeypatch):
    """When deploy-ada.sh has shipped the real snapshot, the overview must
    parse and answer — guards the producer/consumer contract end to end."""
    m = _load()
    _env(monkeypatch, REAL_SNAPSHOT)
    out = asyncio.run(m.run(None))
    assert out["ok"] is True
    assert out["counts"]["total"] > 0
    assert "capabilities" in out["answer"]
