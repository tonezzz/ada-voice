"""Unit tests for opencode/ada_mcp.py bounded lexical SSOT search."""
import importlib.util
import json
import sys
import types
import urllib.error
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
    spec = importlib.util.spec_from_file_location("ada_mcp_ssot_search_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ada_mcp = _load_ada_mcp()

SSOT_DOC = {
    "key": "infrastructure-ssot.host-roles",
    "lang": "en",
    "contentMd": "# Host Roles\n\nhosts:\n  tony-macbook:\n    cpu: i7\n",
    "meta": {
        "original_path": ["infrastructure/ssot.host-roles.yml"],
        "title": ["Host Roles"],
        "updated": ["2026-11-17"],
    },
    "addedAt": 1764000000,
    "updatedAt": 1764000000,
}


def _fts_payload(collection="infrastructure-ssot", hits=None, total=None):
    return {
        "results": hits if hits is not None else [
            {
                "document": SSOT_DOC,
                "score": 2.5,
                "matchedTerms": ["host", "roles"],
            }
        ],
        "total": total if total is not None else 1,
        "searchStats": {"durationMs": 3},
    }


def test_ssot_search_requires_query():
    result = json.loads(ada_mcp.ada_ssot_search("   "))
    assert result["ok"] is False
    assert "query is required" in result["error"]
    assert result["mutations_performed"] == []


def test_ssot_search_rejects_non_allowlisted_collection(monkeypatch):
    def fail_post(*args, **kwargs):
        raise AssertionError("non-allowlisted collection must not be searched")

    monkeypatch.setattr(ada_mcp, "_mddb_post", fail_post)
    result = json.loads(ada_mcp.ada_ssot_search("host", collection="ada-ha-bank-personal"))
    assert result["ok"] is False
    assert "not allowlisted" in result["error"]
    assert result["mutations_performed"] == []


def test_ssot_search_returns_bounded_lexical_hits(monkeypatch):
    calls = []

    def fake_post(path, payload, timeout=5):
        calls.append((path, payload))
        assert path == "fts"
        return _fts_payload(
            collection=payload["collection"],
            hits=(None if payload["collection"] == "infrastructure-ssot" else []),
        )

    monkeypatch.setattr(ada_mcp, "_mddb_post", fake_post)
    raw = ada_mcp.ada_ssot_search("host roles", limit=10)
    result = json.loads(raw)
    assert result["ok"] is True
    assert result["operation"] == "ssot-search"
    assert result["source"] == "mddb-fts"
    assert result["match_mode"] == "lexical"
    assert result["mutations_performed"] == []
    assert [c[1]["collection"] for c in calls] == [
        "infrastructure-ssot", "chaba-glossary",
    ]
    hit = result["results"][0]
    assert hit["path"] == "docs/ssot/infrastructure/ssot.host-roles.yml"
    assert hit["key"] == "infrastructure-ssot.host-roles"
    assert hit["matched_terms"] == ["host", "roles"]
    assert hit["doc"]["title"] == "Host Roles"
    assert "hosts:" in hit["excerpt"]
    assert result["counts"]["returned"] == 1


def test_ssot_search_collection_param_narrows_scope(monkeypatch):
    calls = []

    def fake_post(path, payload, timeout=5):
        calls.append(payload["collection"])
        return _fts_payload(collection=payload["collection"], hits=[], total=0)

    monkeypatch.setattr(ada_mcp, "_mddb_post", fake_post)
    result = json.loads(ada_mcp.ada_ssot_search("host", collection="chaba-glossary"))
    assert result["ok"] is True
    assert calls == ["chaba-glossary"]
    assert result["collections"] == ["chaba-glossary"]
    assert result["results"] == []


def test_ssot_search_limit_is_clamped(monkeypatch):
    seen_limits = []

    def fake_post(path, payload, timeout=5):
        seen_limits.append(payload["limit"])
        return _fts_payload(hits=[], total=0)

    monkeypatch.setattr(ada_mcp, "_mddb_post", fake_post)
    result = json.loads(ada_mcp.ada_ssot_search("x", limit=500))
    assert result["ok"] is True
    assert seen_limits == [ada_mcp.SSOT_SEARCH_MAX_LIMIT] * 2
    assert result["budgets"]["max_results"] == ada_mcp.SSOT_SEARCH_MAX_LIMIT


def test_ssot_search_records_per_collection_errors(monkeypatch):
    def fake_post(path, payload, timeout=5):
        if payload["collection"] == "infrastructure-ssot":
            raise urllib.error.HTTPError("http://mddb/fts", 500, "boom", {}, None)
        return _fts_payload(collection="chaba-glossary", hits=[], total=0)

    monkeypatch.setattr(ada_mcp, "_mddb_post", fake_post)
    result = json.loads(ada_mcp.ada_ssot_search("host"))
    assert result["ok"] is True
    assert any("infrastructure-ssot-fts" in e for e in result["errors"])
    assert result["mutations_performed"] == []


def test_ssot_search_tolerates_malformed_payload(monkeypatch):
    monkeypatch.setattr(ada_mcp, "_mddb_post", lambda *a, **k: ["not", "a", "dict"])
    result = json.loads(ada_mcp.ada_ssot_search("host"))
    assert result["ok"] is True
    assert result["results"] == []
    assert len(result["errors"]) == 2
