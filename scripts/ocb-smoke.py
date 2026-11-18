#!/usr/bin/env python3
"""Live smoke for the ada-control OpenCode bridge (opencode/ada_mcp.py).

Imports the bridge and runs the real tool functions against live
board-api + MDDB: ada_ssot_get (tony-macbook spec), find_card ADA-0172,
_pick_cards, ada_context_pack, and the ada_preflight gate fields.
Each call must return ok=true with mutations_performed=[] in < 8s.

The run is appended as a kind:benchmark doc to the
ada-ha-scenario-reports MDDB collection so smoke history is queryable.
Exit nonzero on failure. Wired hourly via ada-ocb-smoke.timer.

  scripts/ocb-smoke.py [--bridge PATH] [--mddb URL] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import os
import sys
import time
import types
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / "opencode" / "ada_mcp.py"
COLLECTION = "ada-ha-scenario-reports"
CALL_LIMIT_S = 8.0
# ada-ha-scenario-reports is an ops collection — write to the mddb-ops
# store (idc02 :11025, the MDDB_OPS_URL convention). The leader's /add
# embeds inline and times out under embedding-provider rate limits; ops
# has no embedding provider so writes are fast (scenario-report.py note).
DEFAULT_MDDB = "http://100.123.163.11:11025/v1"


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
    spec = importlib.util.spec_from_file_location("ada_mcp_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tool(bridge, name):
    tools = bridge.mcp._tool_manager._tools
    return tools[name].fn


def _post(url: str, payload: dict, timeout: float = 20) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def _check(name):
    """Decorator: time a check, capture exceptions as failures."""
    def wrap(fn):
        def run(bridge):
            t0 = time.monotonic()
            try:
                detail = fn(bridge)
                elapsed = time.monotonic() - t0
                ok = elapsed < CALL_LIMIT_S
                note = detail if ok else f"{detail}; SLOW {elapsed:.1f}s"
                return {"name": name, "ok": ok, "elapsed_s": round(elapsed, 3),
                        "detail": note}
            except Exception as exc:
                return {"name": name, "ok": False,
                        "elapsed_s": round(time.monotonic() - t0, 3),
                        "detail": f"{type(exc).__name__}: {exc}"}
        return run
    return wrap


def _must(doc, what="result"):
    if not isinstance(doc, dict) or doc.get("ok") is not True:
        raise AssertionError(f"{what} not ok: {str(doc)[:300]}")
    mut = doc.get("mutations_performed")
    if mut is not None and mut != []:
        raise AssertionError(f"{what} mutated: {mut}")
    return doc


@_check("ada_bridge_info")
def _c_bridge_info(bridge):
    doc = _must(json.loads(_tool(bridge, "ada_bridge_info")()))
    return f"version={doc['bridge']['version']} tools={doc['bridge']['tools_count']}"


@_check("ada_ssot_get tony-macbook")
def _c_ssot_get(bridge):
    doc = _must(json.loads(_tool(bridge, "ada_ssot_get")(
        collection="infrastructure-ssot",
        key="ssot.host-capacity", selector="tony-macbook")))
    # the doc spells it tony_macbook: — compare normalized like the bridge
    assert "tony-macbook" in bridge._norm_selector_text(doc["content"]), \
        "selector block lost the host"
    assert doc["matched_selector"] is True, "selector did not match a block"
    return (f"key={doc['key']} matched={doc['matched_selector']} "
            f"line={doc['selector_line']} excerpt={doc['excerpt_chars']}c")


@_check("find_card ADA-0172")
def _c_find_card(bridge):
    card = bridge.find_card("ADA-0172")
    assert card.get("ref") == "ADA-0172", f"wrong card: {card.get('id')}"
    return f"{card.get('id')} col={card.get('column')}"


@_check("_pick_cards sweep")
def _c_pick(bridge):
    picks = bridge._pick_cards("sweep", 7, 3)
    assert isinstance(picks, list), "picks not a list"
    ids = [p["card"].get("ref") or p["card"].get("id") for p in picks]
    return f"{len(picks)} picks {ids}"


@_check("ada_context_pack")
def _c_context_pack(bridge):
    doc = _must(json.loads(_tool(bridge, "ada_context_pack")(
        query="ada-ocb bridge board snapshots",
        card_reference="ADA-0172", limit=3)))
    assert doc["card"] is not None, "card snapshot missing from pack"
    return (f"sources={len(doc['allowed_sources'])} "
            f"ssot={len(doc['ssot'])} errors={doc['errors'][:3]}")


@_check("ada_preflight gates")
def _c_preflight(bridge):
    doc = _must(json.loads(_tool(bridge, "ada_preflight")(
        "ADA-0172", selection_confirmation="ADA-0172")))
    gates = doc["start_check"]["gates"]
    for field in ("card_found", "backlog", "no_open_requests",
                  "safe_scope", "typed_dispatch_available"):
        assert field in gates, f"missing gate {field}"
    dc = doc["dispatch_check"]
    assert "verifier" in dc["required_fields"], "verifier gate missing"
    assert doc["workflow_id"].startswith("ada-preflight-")
    return f"ready={doc['ready_for_dispatch']} missing={dc['missing_fields']}"


CHECKS = [_c_bridge_info, _c_ssot_get, _c_find_card, _c_pick,
          _c_context_pack, _c_preflight]


def _markdown(results, all_ok, bridge_path, wall):
    now = datetime.datetime.now().astimezone()
    lines = [
        f"# ocb-smoke {now:%Y-%m-%d %H:%M} — {'PASS' if all_ok else 'FAIL'}",
        "",
        "Live read-only smoke of `opencode/ada_mcp.py` against board-api "
        f"+ MDDB. bridge=`{bridge_path}`, wall={wall:.1f}s.",
        "",
        "| check | elapsed_s | status | detail |",
        "|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['name']} | {r['elapsed_s']} "
            f"| {'PASS' if r['ok'] else 'FAIL'} | {str(r['detail'])[:160]} |")
    if not all_ok:
        lines += ["", "**Failed checks** — investigate before the next deploy."]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bridge", default=str(BRIDGE),
                    help="ada_mcp.py path (default: repo opencode/ copy)")
    ap.add_argument("--mddb",
                    default=os.environ.get("OCB_MDDB_URL")
                    or os.environ.get("MDDB_OPS_URL")
                    or DEFAULT_MDDB)
    ap.add_argument("--dry-run", action="store_true",
                    help="run checks, print the doc, skip the MDDB write")
    args = ap.parse_args()

    bridge = _load_bridge(Path(args.bridge))
    t0 = time.monotonic()
    results = [check(bridge) for check in CHECKS]
    wall = time.monotonic() - t0
    all_ok = all(r["ok"] for r in results)

    for r in results:
        print(f"{'PASS' if r['ok'] else 'FAIL'} {r['name']:<28} "
              f"{r['elapsed_s']:>6}s  {r['detail']}")
    md = _markdown(results, all_ok, args.bridge, wall)

    now = datetime.datetime.now().astimezone()
    key = f"benchmark/ocb-smoke-{now:%Y%m%d-%H%M%S}"
    if args.dry_run:
        print(f"\n[dry-run] would write {COLLECTION}/{key}\n{md}")
        return 0 if all_ok else 1
    try:
        _post(f"{args.mddb.rstrip('/')}/add", {
            "collection": COLLECTION, "key": key, "lang": "en",
            "contentMd": md,
            "meta": {"kind": ["benchmark"], "suite": ["ocb-smoke"],
                     "status": ["pass" if all_ok else "fail"],
                     "ts": [now.isoformat(timespec="seconds")],
                     "bridge": [str(args.bridge)],
                     "checks": [f"{r['name']}:{'pass' if r['ok'] else 'fail'}"
                                for r in results],
                     "metrics": [f"{r['name']}={r['elapsed_s']}s"
                                 for r in results] + [f"wall={wall:.1f}s"]},
        })
    except Exception as exc:
        print(f"\nMDDB write failed: {exc}", file=sys.stderr)
        return 1
    print(f"\nwrote {COLLECTION}/{key}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
