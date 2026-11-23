"""Unit tests for opencode/ada_mcp.py BRIDGE_VERSION + response envelope."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

mcp_pkg = pytest.importorskip("mcp", reason="real mcp package required")

ADA_MCP = Path(__file__).resolve().parents[1] / "opencode" / "ada_mcp.py"


def _load(path: Path, name: str = "ada_mcp_bridge_under_test"):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def ada_mcp():
    return _load(ADA_MCP)


def test_bridge_version_is_nonempty_string(ada_mcp):
    assert isinstance(ada_mcp.BRIDGE_VERSION, str)
    assert ada_mcp.BRIDGE_VERSION.strip()


def test_env_override_wins(ada_mcp, monkeypatch):
    monkeypatch.setenv("ADA_BRIDGE_VERSION", "9.9.9-test")
    assert ada_mcp._resolve_bridge_version() == "9.9.9-test"


def test_version_file_beats_git(ada_mcp, tmp_path, monkeypatch):
    monkeypatch.delenv("ADA_BRIDGE_VERSION", raising=False)
    copy = tmp_path / "ada_mcp.py"
    copy.write_bytes(ADA_MCP.read_bytes())
    (tmp_path / "VERSION").write_text("deploy-2026-11-17\n", encoding="utf-8")
    mod = _load(copy, "ada_mcp_version_file")
    assert mod.BRIDGE_VERSION == "deploy-2026-11-17"


def test_stale_copy_reports_different_version(ada_mcp, tmp_path, monkeypatch):
    """A stale copy outside the repo must not collide with the canonical one."""
    monkeypatch.delenv("ADA_BRIDGE_VERSION", raising=False)
    copy = tmp_path / "ada_mcp.py"
    copy.write_bytes(ADA_MCP.read_bytes() + b"# stale marker\n")
    stale = _load(copy, "ada_mcp_stale_copy")
    assert stale.BRIDGE_VERSION != ada_mcp.BRIDGE_VERSION


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10)


def _init_repo(path: Path) -> None:
    assert _git(path, "init", "-q").returncode == 0
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    assert _git(path, "add", "seed.txt").returncode == 0
    commit = _git(path, "-c", "user.email=t@t", "-c", "user.name=t",
                  "commit", "-qm", "init")
    assert commit.returncode == 0


def test_untracked_copy_inside_checkout_gets_copy_tag(tmp_path, monkeypatch):
    """A loose copy inside a git checkout must not claim that repo's sha.

    `git describe` ignores untracked files, so without the tag the copy
    would report the enclosing repo's exact `git-<sha>` and collide with
    the canonical bridge copy — the incident this guards against.
    """
    monkeypatch.delenv("ADA_BRIDGE_VERSION", raising=False)
    _init_repo(tmp_path)
    copy = tmp_path / "ada_mcp.py"
    copy.write_bytes(ADA_MCP.read_bytes())
    mod = _load(copy, "ada_mcp_untracked_copy")
    assert mod.BRIDGE_VERSION.startswith("git-")
    assert "+copy-" in mod.BRIDGE_VERSION


def test_tracked_copy_inside_checkout_has_no_copy_tag(tmp_path, monkeypatch):
    """A tracked file inside a checkout IS that repo's copy — no tag."""
    monkeypatch.delenv("ADA_BRIDGE_VERSION", raising=False)
    _init_repo(tmp_path)
    copy = tmp_path / "ada_mcp.py"
    copy.write_bytes(ADA_MCP.read_bytes())
    assert _git(tmp_path, "add", "ada_mcp.py").returncode == 0
    commit = _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t",
                  "commit", "-qm", "add bridge")
    assert commit.returncode == 0
    mod = _load(copy, "ada_mcp_tracked_copy")
    assert mod.BRIDGE_VERSION.startswith("git-")
    assert "+copy-" not in mod.BRIDGE_VERSION


def test_tool_response_carries_bridge_envelope(ada_mcp, monkeypatch, tmp_path):
    monkeypatch.setenv("ADA_CONTROL_AUDIT_DIR", str(tmp_path))
    tools = ada_mcp.mcp._tool_manager._tools
    raw = tools["ada_bridge_info"].fn()
    parsed = json.loads(raw)
    assert "bridge" in parsed
    assert parsed["bridge"]["version"] == ada_mcp.BRIDGE_VERSION
    assert parsed["bridge"]["tools_count"] == len(tools)
    assert parsed["ok"] is True


def test_audit_line_records_bridge_version(ada_mcp, monkeypatch, tmp_path):
    monkeypatch.setenv("ADA_CONTROL_AUDIT_DIR", str(tmp_path))
    tools = ada_mcp.mcp._tool_manager._tools
    tools["ada_bridge_info"].fn()
    lines = (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines, "expected an audit line"
    entry = json.loads(lines[-1])
    assert entry["bridge_version"] == ada_mcp.BRIDGE_VERSION
    assert entry["tool"] == "ada_bridge_info"


def test_envelope_leaves_non_dict_results_alone(ada_mcp):
    assert ada_mcp._with_bridge_envelope("not json") == "not json"
    assert ada_mcp._with_bridge_envelope(json.dumps([1, 2])) == json.dumps([1, 2])
    enveloped = ada_mcp._with_bridge_envelope({"ok": True})
    assert enveloped["bridge"]["version"] == ada_mcp.BRIDGE_VERSION
    assert enveloped["ok"] is True
