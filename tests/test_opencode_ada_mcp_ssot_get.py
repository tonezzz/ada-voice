"""Unit tests for opencode/ada_mcp.py bounded SSOT direct-get."""
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
    spec = importlib.util.spec_from_file_location("ada_mcp_ssot_get_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ada_mcp = _load_ada_mcp()

HOST_ROLES_MD = """# SSOT: Host Roles

```yaml
hosts:
  tony-macbook:
    description: portable Linux dev/lab node
    cpu: Intel Core i7-6700HQ
    memory_gb: 16
    tailnet: tony-macbook
    tailscale_ip: 100.77.76.1
  kk-macbook:
    description: secondary macbook node
    cpu: Apple Silicon
```
"""


def test_ssot_key_candidates_normalize_paths_and_extensions():
    candidates = ada_mcp._ssot_key_candidates(
        "infrastructure-ssot",
        "docs/ssot/infrastructure/ssot.host-roles.yml",
    )
    assert "infrastructure-ssot.host-roles" in candidates
    assert "host-roles" in candidates
    assert ada_mcp._ssot_key_candidates("infrastructure-ssot", "host-roles") == [
        "host-roles",
        "infrastructure-ssot.host-roles",
    ]


def test_selector_block_extracts_matching_yaml_block():
    block = ada_mcp._selector_block(HOST_ROLES_MD, "tony-macbook", 12000)
    assert block["matched"] is True
    assert "tony-macbook" in block["text"]
    assert "memory_gb: 16" in block["text"]
    assert "tailscale_ip: 100.77.76.1" in block["text"]
    assert "kk-macbook" not in block["text"]


def test_selector_block_matches_underscore_yaml_keys():
    content = "hosts:\n  tony_macbook:\n    cpu: i7\n    disk_tb: 1\n  other:\n    cpu: arm\n"
    block = ada_mcp._selector_block(content, "host=tony-macbook", 12000)
    assert block["matched"] is True
    assert "tony_macbook" in block["text"]
    assert "disk_tb: 1" in block["text"]
    assert "other:" not in block["text"]


def test_ada_ssot_get_returns_bounded_doc_with_provenance(monkeypatch):
    calls = []

    def fake_get(path, payload, timeout=5):
        calls.append((path, payload))
        assert path == "get"
        assert payload["collection"] == "infrastructure-ssot"
        if payload["key"] != "infrastructure-ssot.host-roles":
            return {}
        return {
            "key": payload["key"],
            "lang": "en",
            "contentMd": HOST_ROLES_MD,
            "meta": {
                "original_path": "docs/ssot/infrastructure/ssot.host-roles.yml",
                "title": "Host Roles",
                "updated": "2026-11-17",
            },
        }

    monkeypatch.setattr(ada_mcp, "_mddb_post", fake_get)
    raw = ada_mcp.ada_ssot_get(
        "infrastructure-ssot",
        "docs/ssot/infrastructure/ssot.host-roles.yml",
        selector="tony-macbook",
        max_chars=12000,
    )
    result = json.loads(raw)
    assert result["ok"] is True
    assert result["key"] == "infrastructure-ssot.host-roles"
    assert result["matched_selector"] is True
    assert "memory_gb: 16" in result["content"]
    assert result["doc"]["path"] == "docs/ssot/infrastructure/ssot.host-roles.yml"
    assert result["mutations_performed"] == []
    assert [call[1]["key"] for call in calls] == [
        "docs/ssot/infrastructure/ssot.host-roles.yml",
        "host-roles",
        "infrastructure-ssot.host-roles",
    ]


def test_ada_ssot_get_rejects_non_allowlisted_collection(monkeypatch):
    def fail_get(*args, **kwargs):
        raise AssertionError("non-allowlisted collection must not be fetched")

    monkeypatch.setattr(ada_mcp, "_mddb_post", fail_get)
    result = json.loads(ada_mcp.ada_ssot_get("ada-ha-bank-personal", "host-roles"))
    assert result["ok"] is False
    assert "not allowlisted" in result["error"]
    assert result["mutations_performed"] == []


def test_ada_ssot_get_reports_missing_key(monkeypatch):
    def fake_get(path, payload, timeout=5):
        import urllib.error
        raise urllib.error.HTTPError(
            "http://mddb/get", 404, "not found", {}, None
        )

    monkeypatch.setattr(ada_mcp, "_mddb_post", fake_get)
    result = json.loads(ada_mcp.ada_ssot_get("infrastructure-ssot", "missing-doc"))
    assert result["ok"] is False
    assert "not found" in result["error"]
    assert result["mutations_performed"] == []
