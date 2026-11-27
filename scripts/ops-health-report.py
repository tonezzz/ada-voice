#!/usr/bin/env python3
"""Ops health reporter — writes report/health-* docs + refreshes the
ops-health CMS page so Ada answers 'what's broken?' from one read.

Checks: mddb http+vector, ada-ha-tony ws, jev-student (8778), open-jev-4b on idc02
systemone, embed proxy. Counts systemd restart churn (restarts in 24h —
the crash-loop signal a single is-active check misses).
"""
import http.client, json, os, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.report_meta import validate_report_meta  # noqa: E402
from backend.mddb_client import is_cms_collection  # noqa: E402

MDDB = os.environ.get(
    "MDDB_BASE_URL", "http://idc03.taila0626a.ts.net:11023/v1").rstrip("/")
MDDB_CMS = os.environ.get(
    "MDDB_CMS_URL", "http://100.102.134.91:11027/v1").rstrip("/")
# MEM-1507: health opens before /v1 write routes are live (~84-104s after an
# OOM restart). Gate on /v1/stats and retry keyed writes with bounded backoff.
WRITE_TIMEOUT = float(os.environ.get("MDDB_HTTP_WRITE_TIMEOUT", "120"))
ROUTE_WAIT_S = float(os.environ.get("MDDB_ROUTE_WAIT_S", "120"))
TRANSIENT_HTTP = (urllib.error.URLError, TimeoutError, ConnectionError,
                  http.client.HTTPException)
RETRYABLE_STATUS = {404, 429, 502, 503, 504}
NOW = datetime.now().astimezone()

def probe(name, fn):
    t0 = time.time()
    try:
        detail = fn()
        return {"name": name, "ok": True, "ms": int((time.time()-t0)*1000), "detail": detail}
    except Exception as e:
        return {"name": name, "ok": False, "ms": int((time.time()-t0)*1000), "detail": str(e)[:120]}

def get(url, timeout=8, json_body=None):
    if json_body is not None:
        req = urllib.request.Request(url, data=json.dumps(json_body).encode(),
                                     headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

def restarts(unit):
    try:
        out = subprocess.check_output(
            ["journalctl", "--user", "-u", unit, "--since", "-24h",
             "--no-pager", "-o", "json", "--output-fields", "MESSAGE"],
            text=True, timeout=15)
        n = out.count("Started ") + out.count("start-pre")
        return n
    except Exception:
        return -1

def is_active(unit):
    try:
        return subprocess.check_output(
            ["systemctl", "--user", "is-active", unit],
            text=True, timeout=5).strip() == "active"
    except Exception:
        return False

checks = [
    probe("mddb", lambda: get("http://idc03.taila0626a.ts.net:11023/health")),
    probe("mddb-vector", lambda: get(MDDB + "/vector-search", 15, {
        "collection": "ada-ha-bank-general", "query": "health", "topK": 1})),
    probe("mddb-cms-vector", lambda: get(MDDB_CMS + "/vector-search", 15, {
        "collection": "ada-cms-pages", "query": "health", "topK": 1})),
    probe("ada-ha-tony-http", lambda:
            urllib.request.urlopen("http://127.0.0.1:8002/", timeout=8).status),
    probe("jev-student", lambda: get("http://idc03.taila0626a.ts.net:8778/health")),
    probe("open-jev-4b-idc02", lambda: get("http://100.123.163.11:8777/v1/systemone", 30, {
        "state": "probe", "questions": {"q": {"type": "noul",
        "instructions": "x", "criteria": {"true": "t", "false": "f"}}}})),
]

# long-running units vs timer-driven oneshots — inactive is only bad
# for the first kind. gev-gemini lives on tony-dell, not here.
SERVICES = ["mddb", "ada-ha-tony", "jev-student"]
TIMERS = ["news-flood", "ada-scenario-smoke", "jev-bench",
          "ada-memory-sync", "doc-mirror"]
svc_rows = []
for u in SERVICES:
    active = is_active(u)
    n = restarts(u)
    flag = " ⚠" if (not active or n > 10) else ""
    svc_rows.append({"unit": u, "active": active, "restarts_24h": n, "flag": flag})
for t in TIMERS:
    on = is_active(t + ".timer")
    svc_rows.append({"unit": t + ".timer", "active": on, "restarts_24h": "-",
                     "flag": " ⚠" if not on else ""})

bad = [c for c in checks if not c["ok"]]
bad_svc = [s for s in svc_rows if s["flag"]]
verdict = "healthy" if not bad and not bad_svc else (
          "degraded" if len(bad) + len(bad_svc) <= 2 else "failing")

lines = [f"# Ops health — {NOW:%Y-%m-%d %H:%M}\n",
         f"**{verdict.upper()}**\n"]
if bad or bad_svc:
    lines.append("## Flagged\n")
    for c in bad:
        lines.append(f"- **{c['name']}** — {c['detail']}")
    for s in bad_svc:
        lines.append(f"- **{s['unit']}** — active={s['active']} restarts24h={s['restarts_24h']}")
    lines.append("")
lines.append("## Probes\n")
lines.append("| probe | ok | ms | detail |")
lines.append("|---|---|---|---|")
for c in checks:
    lines.append(f"| {c['name']} | {'yes' if c['ok'] else 'NO'} | {c['ms']} | {str(c['detail'])[:60]} |")
lines.append("\n## Services\n")
lines.append("| unit | active | restarts (24h) |")
lines.append("|---|---|---|")
for s in svc_rows:
    lines.append(f"| {s['unit']} | {s['active']} | {s['restarts_24h']}{s['flag']} |")
md = "\n".join(lines)

def wait_for_routes(base):
    """GET /v1/stats only succeeds once mddb's full API is listening."""
    deadline = time.time() + ROUTE_WAIT_S
    while True:
        try:
            if get(base + "/stats", timeout=5) is not None:
                return True
        except urllib.error.HTTPError:
            pass
        except TRANSIENT_HTTP:
            pass
        left = deadline - time.time()
        if left <= 0:
            return False
        print(f"mddb /v1 routes not ready at {base}; waiting {min(5, left):.0f}s",
              file=sys.stderr)
        time.sleep(min(5, left))


def post(path, payload):
    base = MDDB_CMS if is_cms_collection(str(payload.get("collection", ""))) else MDDB
    if not wait_for_routes(base):
        raise RuntimeError(f"mddb /v1 routes not ready within {ROUTE_WAIT_S}s "
                           f"at {base}; refusing POST {path}")
    for attempt in range(1, 4):
        req = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=WRITE_TIMEOUT) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRYABLE_STATUS or attempt == 3:
                raise
            print(f"POST {path} HTTP {exc.code}; attempt {attempt}/3",
                  file=sys.stderr)
        except TRANSIENT_HTTP as exc:
            if attempt == 3:
                raise
            print(f"POST {path} {type(exc).__name__}; attempt {attempt}/3",
                  file=sys.stderr)
        time.sleep(2 ** attempt)
    raise RuntimeError(f"POST {path} exhausted retries")

ts = NOW.strftime("%Y%m%d-%H%M%S")
meta = {"kind": ["report"], "domain": ["health"], "suite": ["ops-health"],
        "summary": [f"{verdict}: {len(bad)+len(bad_svc)} flagged of "
                    f"{len(checks)+len(svc_rows)} checks"],
        "fresh_for": ["1h"],
        # probes + systemd reads are direct measurements
        "confidence": ["high"],
        "ts": [NOW.isoformat(timespec="seconds")]}
cms_meta = {**meta, "kind": ["report"], "slug": ["ops-health"],
            "title": [f"Ops health — {NOW:%Y-%m-%d %H:%M}"],
            "format": ["markdown"], "instance": ["idc01"],
            "updated": [NOW.isoformat(timespec="seconds")],
            "timeline": [f"{NOW.isoformat(timespec='minutes')}: {verdict} "
                         f"({len(bad)+len(bad_svc)} flags)"]}
# Report meta contract (ssot.apps.ada-cms-reports.yml) — refuse to publish
# a page missing required fields; warnings log but don't block.
check = validate_report_meta(cms_meta)
if not check["ok"]:
    print(f"meta contract violation, not publishing: {check['missing']}",
          file=sys.stderr)
    sys.exit(1)
for w in check["warnings"]:
    print(f"meta warning: {w}", file=sys.stderr)
post("/add", {"collection": "ada-ha-scenario-reports",
              "key": f"report/health-{ts}", "lang": "en",
              "contentMd": md, "meta": meta})
post("/add", {"collection": "ada-cms-pages", "key": "ops-health", "lang": "en",
              "contentMd": md, "meta": cms_meta})
print(f"{verdict} — {len(bad)+len(bad_svc)} flagged, published report/health-{ts}")
