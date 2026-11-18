#!/usr/bin/env python3
"""Benchmark the ada-control bridge call log -> kind:benchmark MDDB doc.

Reads ~/.local/share/ada-control/calls.jsonl (the audit log written by
opencode/ada_mcp.py's _install_call_audit wrapper) and reports per-tool
call counts, ok-rate, p50/p95 elapsed, and error classes, so latency and
ok trends are measurable across deploys. Appends one doc to the
ada-ha-scenario-reports collection. Exit nonzero only on hard failure.

  scripts/ocb-bench.py [--calls PATH] [--hours N] [--mddb URL] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.request
from pathlib import Path

COLLECTION = "ada-ha-scenario-reports"
# Ops collection -> mddb-ops (idc02 :11025, MDDB_OPS_URL convention) —
# the leader's /add embeds inline and can time out; see ocb-smoke.py.
DEFAULT_MDDB = "http://100.123.163.11:11025/v1"

CALLS_CANDIDATES = (
    "~/.local/share/ada-control/calls.jsonl",
    # ada-code container HOME mount (bridge actually runs in podman)
    "~/.local/share/ada-code/home/.local/share/ada-control/calls.jsonl",
)


def _resolve_calls(cli_arg: str) -> Path:
    if cli_arg:
        return Path(cli_arg).expanduser()
    env = os.environ.get("ADA_CONTROL_CALLS", "").strip()
    if env:
        return Path(env).expanduser()
    for candidate in CALLS_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.is_file():
            return path
    return Path(CALLS_CANDIDATES[0]).expanduser()


def _parse_ts(raw: str) -> datetime.datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S%Z"):
        try:
            return datetime.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _error_class(text: str) -> str:
    t = text.lower()
    if "timed out" in t or "timeout" in t:
        return "timeout"
    if "http error 4" in t:
        return "http_4xx"
    if "http error 5" in t:
        return "http_5xx"
    if "urlopen error" in t or "connection" in t or "refused" in t:
        return "connect"
    if "no board card matched" in t or "matched" in t and "cards" in t:
        return "lookup"
    if "not allowlisted" in t or "required" in t or "confirmation" in t:
        return "gate"
    return "other"


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


def _read_entries(path: Path, since: datetime.datetime | None):
    entries, malformed = [], 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return entries, malformed
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(entry, dict) or "tool" not in entry:
            continue  # startup/meta lines carry no tool field
        if since is not None:
            ts = _parse_ts(str(entry.get("ts") or ""))
            if ts is None or ts < since:
                continue
        entries.append(entry)
    return entries, malformed


def _summarise(entries: list[dict]) -> dict:
    tools: dict[str, dict] = {}
    for entry in entries:
        name = str(entry.get("tool"))
        row = tools.setdefault(name, {
            "calls": 0, "ok": 0, "fail": 0,
            "elapsed": [], "errors": {}})
        row["calls"] += 1
        if entry.get("error"):
            row["fail"] += 1
            cls = _error_class(str(entry["error"]))
            row["errors"][cls] = row["errors"].get(cls, 0) + 1
        elif entry.get("ok") is False:
            row["fail"] += 1
            row["errors"]["ok_false"] = row["errors"].get("ok_false", 0) + 1
        else:
            row["ok"] += 1
        if isinstance(entry.get("elapsed_s"), (int, float)):
            row["elapsed"].append(float(entry["elapsed_s"]))
        for soft in entry.get("errors") or []:
            cls = f"soft:{str(soft).split(':', 1)[0]}"
            row["errors"][cls] = row["errors"].get(cls, 0) + 1
    out = {}
    for name, row in sorted(tools.items()):
        errors = dict(sorted(row["errors"].items(), key=lambda kv: -kv[1]))
        out[name] = {
            "calls": row["calls"], "ok": row["ok"], "fail": row["fail"],
            "ok_rate": round(row["ok"] / row["calls"], 4) if row["calls"] else 0,
            "p50_s": round(_pct(row["elapsed"], 0.5), 3),
            "p95_s": round(_pct(row["elapsed"], 0.95), 3),
            "errors": errors,
        }
    return out


def _markdown(per_tool, total, window_label, calls_path, malformed):
    now = datetime.datetime.now().astimezone()
    lines = [
        f"# ocb-bench {now:%Y-%m-%d %H:%M}",
        "",
        f"call audit `{calls_path}` — {total} calls in {window_label}, "
        f"{malformed} malformed line(s) skipped.",
        "",
        "| tool | calls | ok% | p50_s | p95_s | error classes |",
        "|---|---|---|---|---|---|",
    ]
    for name, row in per_tool.items():
        errs = ",".join(f"{k}={v}" for k, v in row["errors"].items()) or "-"
        lines.append(
            f"| {name} | {row['calls']} | {row['ok_rate'] * 100:.0f}% "
            f"| {row['p50_s']} | {row['p95_s']} | {errs} |")
    if not per_tool:
        lines.append("| (no calls) | 0 | - | - | - | - |")
    return "\n".join(lines)


def _post(url: str, payload: dict, timeout: float = 20) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--calls", default="",
                    help="calls.jsonl path (default: ~/.local/share/"
                         "ada-control/calls.jsonl, else the ada-code "
                         "container HOME mount)")
    ap.add_argument("--hours", type=float, default=24,
                    help="only count entries newer than N hours (0 = all)")
    ap.add_argument("--mddb",
                    default=os.environ.get("OCB_MDDB_URL")
                    or os.environ.get("MDDB_OPS_URL")
                    or DEFAULT_MDDB)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    calls_path = _resolve_calls(args.calls)
    since = None
    if args.hours and args.hours > 0:
        since = datetime.datetime.now().astimezone() - datetime.timedelta(
            hours=args.hours)
    entries, malformed = _read_entries(calls_path, since)
    per_tool = _summarise(entries)
    window_label = ("last %gh" % args.hours) if since else "all time"
    md = _markdown(per_tool, len(entries), window_label, calls_path, malformed)

    flat_p95 = max((r["p95_s"] for r in per_tool.values()), default=0.0)
    ok_rate = (sum(r["ok"] for r in per_tool.values()) / len(entries)
               if entries else 0.0)
    if args.dry_run:
        print(md)
        return 0

    now = datetime.datetime.now().astimezone()
    key = f"benchmark/ocb-bench-{now:%Y%m%d-%H%M%S}"
    try:
        _post(f"{args.mddb.rstrip('/')}/add", {
            "collection": COLLECTION, "key": key, "lang": "en",
            "contentMd": md,
            "meta": {"kind": ["benchmark"], "suite": ["ocb-bench"],
                     "status": ["no-data" if not entries else "pass"],
                     "ts": [now.isoformat(timespec="seconds")],
                     "calls": [str(len(entries))],
                     "tools": [str(len(per_tool))],
                     "window": [window_label],
                     "ok_rate": [f"{ok_rate:.3f}"],
                     "p95_s_max": [f"{flat_p95}"],
                     "tools_detail": [
                         f"{n}:{r['calls']}c ok={r['ok_rate']} "
                         f"p95={r['p95_s']}s"
                         for n, r in per_tool.items()]},
        })
    except Exception as exc:
        print(f"MDDB write failed: {exc}", file=sys.stderr)
        print(md)
        return 1
    print(f"wrote {COLLECTION}/{key} ({len(entries)} calls, "
          f"{len(per_tool)} tools)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
