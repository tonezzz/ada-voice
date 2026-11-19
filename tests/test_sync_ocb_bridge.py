"""Tests for scripts/sync-ocb-bridge.sh.

The script manages a dedicated ff-only worktree (the "bridge checkout") that
serves opencode/ada_mcp.py to OpenCode/OpenChamber MCP clients. Each test
builds a tiny fake ada-voice setup in tmp_path:

    src/    # stand-in for github origin (commits land here)
    main/   # the ada-voice checkout owning the worktree (origin -> src)
    bridge/ # the managed bridge checkout the script creates/syncs

Required behavior under test: refuse (exit 2, touch nothing) a dirty or
diverged bridge checkout, fast-forward a stale one, report in --dry-run.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git not on PATH")

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "sync-ocb-bridge.sh"


def _git(cwd, *args, check=True):
    proc = subprocess.run(
        ["git", "-C", str(cwd),
         "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd}:\n{proc.stderr}")
    return proc


def _rev(cwd, ref="HEAD"):
    return _git(cwd, "rev-parse", ref).stdout.strip()


def _commit_file(cwd, rel, content):
    p = Path(cwd) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _git(cwd, "add", rel)
    _git(cwd, "commit", "-qm", f"update {rel}")


@pytest.fixture()
def env(tmp_path):
    """Fake ada-voice layout; returns (env dict, src, main, bridge paths)."""
    src = tmp_path / "src"
    _git(tmp_path, "init", "-b", "main", src)
    _commit_file(src, "opencode/ada_mcp.py", "# bridge v1\n")
    _commit_file(src, "README.md", "v1\n")

    main = tmp_path / "main"
    _git(tmp_path, "clone", "-q", src, main)

    bridge = tmp_path / "bridge"
    e = dict(os.environ)
    e.update({
        "ADA_VOICE_REPO": str(main),
        "ADA_BRIDGE_CHECKOUT": str(bridge),
        "ADA_REMOTE": "origin",
        "ADA_BRANCH": "main",
    })
    return e, src, main, bridge


def _run(env, *args):
    return subprocess.run(["bash", str(SCRIPT), *args],
                          capture_output=True, text=True, env=env)


def test_creates_missing_checkout_at_origin_main(env):
    e, src, _main, bridge = env
    proc = _run(e)
    assert proc.returncode == 0, proc.stderr
    assert "creating worktree" in proc.stdout
    assert (bridge / "opencode" / "ada_mcp.py").is_file()
    assert _rev(bridge) == _rev(src)
    # detached HEAD — the checkout tracks the commit, not a local branch
    assert _git(bridge, "symbolic-ref", "-q", "HEAD",
                check=False).returncode != 0


def test_fast_forwards_stale_checkout(env):
    e, src, _main, bridge = env
    assert _run(e).returncode == 0
    stale = _rev(bridge)

    _commit_file(src, "opencode/ada_mcp.py", "# bridge v2\n")
    proc = _run(e)
    assert proc.returncode == 0, proc.stderr
    assert "fast-forwarded" in proc.stdout
    assert _rev(bridge) == _rev(src) != stale
    assert (bridge / "opencode" / "ada_mcp.py").read_text() == "# bridge v2\n"


def test_already_current_is_noop(env):
    e, _src, _main, bridge = env
    assert _run(e).returncode == 0
    proc = _run(e)
    assert proc.returncode == 0
    assert "already at" in proc.stdout


def test_dry_run_reports_ff_but_does_not_move(env):
    e, src, _main, bridge = env
    assert _run(e).returncode == 0
    stale = _rev(bridge)
    _commit_file(src, "README.md", "v2\n")

    proc = _run(e, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "would fast-forward" in proc.stdout
    assert _rev(bridge) == stale  # untouched

    # and a real run still performs the move
    assert _run(e).returncode == 0
    assert _rev(bridge) == _rev(src)


@pytest.mark.parametrize("dirty_rel", ["opencode/ada_mcp.py", "README.md"])
def test_refuses_dirty_tree(env, dirty_rel):
    e, _src, _main, bridge = env
    assert _run(e).returncode == 0
    head = _rev(bridge)
    (bridge / dirty_rel).write_text("hand edit — never valid here\n")

    proc = _run(e)
    assert proc.returncode == 2
    assert "REFUSE" in proc.stderr
    assert "dirty" in proc.stderr
    # nothing was touched: HEAD and the hand edit are both still there
    assert _rev(bridge) == head
    assert (bridge / dirty_rel).read_text() == "hand edit — never valid here\n"


def test_refuses_dirty_tree_in_dry_run(env):
    e, _src, _main, bridge = env
    assert _run(e).returncode == 0
    (bridge / "opencode" / "ada_mcp.py").write_text("dirty\n")
    proc = _run(e, "--dry-run")
    assert proc.returncode == 2
    assert "REFUSE" in proc.stderr


def test_refuses_local_commits(env):
    e, _src, _main, bridge = env
    assert _run(e).returncode == 0
    _commit_file(bridge, "local.txt", "not from origin\n")

    proc = _run(e)
    assert proc.returncode == 2
    assert "REFUSE" in proc.stderr
    assert "local commit" in proc.stderr


def test_refuses_nonempty_nongit_path(env):
    e, _src, _main, bridge = env
    bridge.mkdir()
    (bridge / "stray.txt").write_text("occupier\n")
    proc = _run(e)
    assert proc.returncode == 2
    assert (bridge / "stray.txt").read_text() == "occupier\n"
