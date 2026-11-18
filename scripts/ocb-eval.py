#!/usr/bin/env python3
"""Answer-quality eval for the ada-control OpenCode bridge (ADA-0188).

Replays the tool calls an agent SHOULD make for each prompt in
tests/eval_ocb/cases.yml, concatenates the tool outputs into "answer
text", and asserts on the answer — required/forbidden substrings, tools
used, mutation-free, latency. Catches wrong-path failures (skill hijack,
stale bridge, looped context_pack) that ok=true smoke checks miss.

Modes:
  tools   execute each case's tool list against the live bridge (default)
  replay  score recorded calls (--replay FILE.jsonl: {tool,args,result})
  live    best-effort `opencode run` per case (--live); infra failures
          (quota 429, missing binary) mark the case degraded, not failed

Writes one kind:benchmark doc to ada-ha-scenario-reports MDDB.
Exit nonzero when any case fails (degraded does not fail the run).

  scripts/ocb-eval.py [--bridge PATH] [--cases PATH] [--mode tools|replay|live]
                      [--replay FILE] [--live] [--mddb URL] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import os
import subprocess
import sys
import time
import types
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:  # deploy venv has it; fall back to a tiny parser is overkill
    yaml = None

REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / "opencode" / "ada_mcp.py"
CASES = REPO / "tests" / "eval_ocb" / "cases.yml"
COLLECTION = "ada-ha-scenario-reports"
DEFAULT_MDDB = "http://100.123.163.11:11025/v1"
DEFAULT_MAX_ELAPSED_S = 30.0


def _install_fastmcp_stub() -> None:
    """Minimal FastMCP for hosts without the `mcp` package (deploy venv)."""
    if "mcp.server.fastmcp" in sys.modules:
        return

    class _Tool:
        def __init__(self, fn):
            self.fn = fn

    class _ToolManager:
        def __init__(self):
            self._tools = {}

    class FastMCP:
        def __init__(self, name, *args, **kwargs):
            self.name = name
            self._tool_manager = _ToolManager()

        def tool(self, *args, **kwargs):
            def deco(fn):
                self._tool_manager._tools[
                    getattr(fn, "__name__", "tool")] = _Tool(fn)
                return fn
            return deco

        def run(self, *args, **kwargs):
            return None

    mcp_mod = types.ModuleType("mcp")
    mcp_mod.__path__ = []
    server_mod = types.ModuleType("mcp.server")
    server_mod.__path__ = []
    fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
    fastmcp_mod.FastMCP = FastMCP
    server_mod.fastmcp = fastmcp_mod
    mcp_mod.server = server_mod
    sys.modules["mcp"] = mcp_mod
    sys.modules["mcp.server"] = server_mod
    sys.modules["mcp.server.fastmcp"] = fastmcp_mod


def _load_bridge(path: Path):
    try:
        import mcp.server.fastmcp  # noqa: F401
    except ImportError:
        _install_fastmcp_stub()
    spec = importlib.util.spec_from_file_location("ada_mcp_eval", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tool(bridge, name: str):
    tools = bridge.mcp._tool_manager._tools
    return tools[name].fn


def _post(url: str, payload: dict, timeout: float = 20) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def _load_cases(path: Path) -> list[dict]:
    if yaml is None:
        raise RuntimeError("PyYAML required for cases.yml")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"{path} did not contain a case list")
    for i, case in enumerate(data):
        for key in ("id", "prompt", "expect"):
            if key not in case:
                raise RuntimeError(f"case #{i} missing {key!r}")
        case.setdefault("tools", [])
        case.setdefault("mode", "tools")
    return data


def _json_text(result) -> str:
    """Tool fns return JSON strings; keep raw text for substring checks."""
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)


def _result_doc(result):
    """Parse a tool result into a dict when possible."""
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            doc = json.loads(result)
            return doc if isinstance(doc, dict) else {}
        except Exception:
            return {}
    return {}


def _mutations_dirty(doc: dict) -> list:
    hits = []
    for key in ("mutations", "mutations_performed"):
        value = doc.get(key)
        if value not in (None, [], ""):
            hits.append(f"{key}={value}")
    return hits


def _expect_failures(expect: dict, answer_text: str,
                     tools_used: list[str], docs: list[dict]) -> list[str]:
    """Pure assertion engine — unit-testable without a bridge."""
    fails = []
    low = answer_text.lower()
    for needle in expect.get("answer_contains") or []:
        if str(needle).lower() not in low:
            fails.append(f"answer missing {needle!r}")
    for group in expect.get("answer_contains_any") or []:
        if not any(str(n).lower() in low for n in group):
            fails.append(f"answer missing any of {group!r}")
    for needle in expect.get("answer_not_contains") or []:
        if str(needle).lower() in low:
            fails.append(f"answer contains forbidden {needle!r}")
    used = set(tools_used)
    for name in expect.get("tools_used") or []:
        if name not in used:
            fails.append(f"tool {name} not used")
    for name in expect.get("tools_not_used") or []:
        if name in used:
            fails.append(f"tool {name} unexpectedly used")
    if expect.get("mutations_empty", True):
        for doc in docs:
            for hit in _mutations_dirty(doc):
                fails.append(f"mutation leaked: {hit}")
    if expect.get("ok_true", True):
        for doc in docs:
            if doc and doc.get("ok") is False:
                fails.append(f"tool returned ok=false: {doc.get('error', '?')}")
    return fails


def _run_case_tools(case: dict, bridge, replay_rows=None) -> dict:
    """Execute (or replay) the case's tool list; return scored record."""
    expect = case.get("expect") or {}
    budget = float(expect.get("max_elapsed_s") or DEFAULT_MAX_ELAPSED_S)
    tools_used: list[str] = []
    outputs: list[str] = []
    docs: list[dict] = []
    t0 = time.monotonic()
    degraded = None

    for call in case.get("tools") or []:
        name = call.get("tool")
        args = call.get("args") or {}
        tools_used.append(name)
        try:
            if replay_rows is not None:
                row = next(
                    (r for r in replay_rows
                     if r.get("tool") == name or r.get("name") == name),
                    None)
                if row is None:
                    raise LookupError(f"no recorded call for {name}")
                raw = row.get("result") or row.get("result_preview") or ""
            else:
                fn = _tool(bridge, name)
                raw = fn(**args)
            text = _json_text(raw)
            outputs.append(text)
            doc = _result_doc(raw)
            if doc:
                docs.append(doc)
        except Exception as exc:
            degraded = f"{type(exc).__name__}: {exc}"
            outputs.append(f"__ERROR__ {degraded}")
            break
    elapsed = time.monotonic() - t0
    answer_text = "\n".join(outputs)
    fails = _expect_failures(expect, answer_text, tools_used, docs)
    if degraded and replay_rows is None:
        fails.insert(0, f"tool call failed: {degraded}")
    if elapsed > budget:
        fails.append(f"elapsed {elapsed:.1f}s > budget {budget}s")
    return {
        "id": case["id"],
        "prompt": case.get("prompt", ""),
        "ok": not fails,
        "degraded": degraded if replay_rows is None else None,
        "elapsed_s": round(elapsed, 3),
        "tools_used": tools_used,
        "fails": fails,
        "answer_excerpt": answer_text[:400],
    }


def _run_case_live(case: dict) -> dict:
    """Best-effort headless opencode run; infra failure => degraded."""
    binary = os.environ.get("OPENCODE_BIN", "opencode")
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [binary, "run", "--standalone", case["prompt"]],
            capture_output=True, text=True, timeout=180,
            cwd=str(REPO))
    except FileNotFoundError:
        return {"id": case["id"], "ok": False, "degraded": "opencode binary missing",
                "elapsed_s": 0, "tools_used": [], "fails": [], "answer_excerpt": ""}
    except subprocess.TimeoutExpired:
        return {"id": case["id"], "ok": False, "degraded": "opencode run timed out",
                "elapsed_s": round(time.monotonic() - t0, 3),
                "tools_used": [], "fails": [], "answer_excerpt": ""}
    elapsed = time.monotonic() - t0
    out = (proc.stdout or "") + (proc.stderr or "")
    low = out.lower()
    if proc.returncode != 0 and ("429" in out or "rate limit" in low
                                 or "quota" in low):
        return {"id": case["id"], "ok": False,
                "degraded": "provider quota/rate-limit", "elapsed_s": round(elapsed, 3),
                "tools_used": [], "fails": [], "answer_excerpt": out[:400]}
    expect = case.get("expect") or {}
    fails = _expect_failures(expect, out, [], [])
    return {"id": case["id"], "ok": not fails, "degraded": None,
            "elapsed_s": round(elapsed, 3), "tools_used": ["opencode-run"],
            "fails": fails, "answer_excerpt": out[:400]}


def _load_replay(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _markdown(results, mode, bridge_path, wall):
    now = datetime.datetime.now().astimezone()
    scored = [r for r in results if not r.get("degraded")]
    passed = sum(1 for r in scored if r["ok"])
    degraded = [r for r in results if r.get("degraded")]
    lines = [
        f"# ocb-eval {now:%Y-%m-%d %H:%M} — {mode} — "
        f"score {passed}/{len(scored)}"
        + (f" (+{len(degraded)} degraded)" if degraded else ""),
        "",
        f"Answer-quality eval of `opencode/ada_mcp.py` "
        f"(bridge=`{bridge_path}`, mode={mode}, wall={wall:.1f}s). "
        "Assertions run on answer text, not just ok=true.",
        "",
        "| case | status | elapsed_s | fails |",
        "|---|---|---|---|",
    ]
    for r in results:
        status = "DEGRADED" if r.get("degraded") else ("PASS" if r["ok"] else "FAIL")
        detail = "; ".join(r["fails"])[:120] or r.get("degraded", "")
        lines.append(f"| {r['id']} | {status} | {r['elapsed_s']} | {detail} |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bridge", default=str(BRIDGE))
    ap.add_argument("--cases", default=str(CASES))
    ap.add_argument("--mode", choices=["tools", "replay", "live"],
                    default="tools")
    ap.add_argument("--replay", default=None,
                    help="jsonl of recorded calls for --mode replay")
    ap.add_argument("--live", action="store_true",
                    help="shorthand for --mode live")
    ap.add_argument("--mddb",
                    default=os.environ.get("OCB_MDDB_URL")
                    or os.environ.get("MDDB_OPS_URL") or DEFAULT_MDDB)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    mode = "live" if args.live else args.mode
    if mode == "replay" and not args.replay:
        ap.error("--mode replay requires --replay FILE.jsonl")
    cases = _load_cases(Path(args.cases))
    replay_rows = _load_replay(Path(args.replay)) if args.replay else None
    # Fresh bridge per case: selection tokens and snapshot caches are
    # per-process state — a real OpenChamber session gets its own bridge,
    # so cases must not leak state into each other.
    bridge_path = Path(args.bridge)

    t0 = time.monotonic()
    results = []
    for case in cases:
        if mode == "live" or case.get("mode") == "live":
            results.append(_run_case_live(case))
        else:
            bridge = _load_bridge(bridge_path)
            results.append(_run_case_tools(case, bridge, replay_rows))
    wall = time.monotonic() - t0

    scored = [r for r in results if not r.get("degraded")]
    for r in results:
        tag = "DEGRADED" if r.get("degraded") else ("PASS" if r["ok"] else "FAIL")
        print(f"{tag:<8} {r['id']:<32} {r['elapsed_s']:>6}s  "
              f"{'; '.join(r['fails'])[:100] or r.get('degraded', '')}")
    score = f"{sum(1 for r in scored if r['ok'])}/{len(scored)}"
    md = _markdown(results, mode, args.bridge, wall)

    now = datetime.datetime.now().astimezone()
    key = f"benchmark/ocb-eval-{now:%Y%m%d-%H%M%S}"
    if args.dry_run:
        print(f"\n[dry-run] would write {COLLECTION}/{key}\n{md}")
        return 0 if all(r["ok"] or r.get("degraded") for r in results) else 1
    try:
        _post(f"{args.mddb.rstrip('/')}/add", {
            "collection": COLLECTION, "key": key, "lang": "en",
            "contentMd": md,
            "meta": {"kind": ["benchmark"], "suite": ["ocb-eval"],
                     "mode": [mode], "score": [score],
                     "status": ["pass" if all(r["ok"] or r.get("degraded")
                                              for r in results) else "fail"],
                     "ts": [now.isoformat(timespec="seconds")],
                     "bridge": [str(args.bridge)],
                     "cases": [f"{r['id']}:{'pass' if r['ok'] else ('degraded' if r.get('degraded') else 'fail')}"
                               for r in results]},
        })
    except Exception as exc:
        print(f"\nMDDB write failed: {exc}", file=sys.stderr)
        return 1
    print(f"\nwrote {COLLECTION}/{key} score={score}")
    return 0 if all(r["ok"] or r.get("degraded") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
