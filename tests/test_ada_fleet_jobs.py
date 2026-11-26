"""ada_fleet_jobs tools.d — report-verb read path (card
ada-voice-fleet-jobs-verb; contract chaba
docs/kb/ada-report-verb-contract.md). No network: a fake mddb returns a
fixture fleet-jobs-report doc; covers the contract's fresh / stale /
missing freshness gate and the focus= section answers."""
import asyncio
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

MOD = (Path(__file__).resolve().parents[1]
       / "backend" / "tools.d" / "ada_fleet_jobs.py")


def _load():
    spec = importlib.util.spec_from_file_location("ada_fleet_jobs_tool", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# Current producer layout (scripts/fleet-jobs-report.py render_report):
# '## Fleet Claim Totals', '## Runner Fleet', per-host '### Timer States'
# + bad-timer table, '## Lanes' with lane/spend tables.
_MD = """# Fleet Jobs Report - 20261126

## Fleet Claim Totals
- Queued: 11
- Claimed: 0
- Running: 7
- Done: 691
- Failed: 21
- Unassigned queued: 10

## Runner Fleet (board-api /queue)
- Queued claimable: 11 · Claimed/in-flight: 7 · Human-waiting: 3

| Runner | Heartbeat age | Stale | Active work |
|---|---:|---|---|
| idc02 | 262s | no | cline-card-review-lane, dispatch-triage |
| idc03 | 33s | no | — |
| tony-omen | 901s | yes | — |

## Host: idc02

### Timer States
- ok 23 · late 0 · failing 2 · off 3

| timer | state | last run | result |
|---|---|---|---|
| `ada-jobs-consultant` | failing | 2026-11-26T00:00Z | failed |
| `ada-ocb-smoke` | failing | 2026-11-26T00:00Z | failed |

## Host: idc03

### Timer States
- ok 100 · late 1 · failing 12 · off 6

| timer | state | last run | result |
|---|---|---|---|
| `mddb-backup` | failing | 2026-11-26T00:00Z | failed |
| `log-shipper` | late | 2026-11-26T00:00Z | late |

## Lanes
### Action Status by Lane
| Lane | Queued | Claimed | Running | Done | Failed |
|---|---:|---:|---:|---:|---:|
| codemod | 3 | 0 | 0 | 44 | 6 |
| devin | 4 | 0 | 4 | 52 | 5 |
| free_lane | 1 | 0 | 0 | 18 | 2 |

### LiteLLM Spend (trailing 24h)
| Key | Model | Calls | Tokens | Spend (USD) |
|---|---|---:|---:|---:|
| <master> | gpt-4o | 10 | 5000 | $1.23 |
| team-key | claude-x | 4 | 2000 | $0.45 |
"""

# 2026-11-20 page generation: '## Live Queue', emoji timer counts,
# 'Attention timers' — the consumer must still parse it.
_MD_LEGACY = """# Fleet Jobs Report - 20261120

## Fleet Claim Totals (board /cards)
- Queued: 11
- Claimed: 0
- Running: 7
- Done: 691
- Failed: 21
- Unassigned queued: 10

## Live Queue (board-api /queue)
- Claimable queued: 11 · In-flight claimed: 7 · Human-waiting: 3

| Host | Heartbeat age | Stale | Active units |
|---|---|---|---|
| idc02 | 262s | no | 3 |

## Host: idc02

- Runner heartbeat: 262s old
- Observed timers: 28 total — 🟢 23 · 🟡 0 · 🔴 2 · ⬛ 3
- Claims: queued 1 · claimed 0 · running 3 · done 91 · failed 3
- Attention timers: `ada-jobs-consultant` (missing), `ada-ocb-smoke` (failing)
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _doc(md: str, updated: datetime, **meta_over) -> dict:
    meta = {
        "updated": [_iso(updated)],
        "fresh_for": ["4h"],
        "summary": ["11 hosts; queue 11 claimable/7 in-flight; "
                    "7 actions running; 2 spend rows"],
        "title": ["Fleet Jobs Report - fixture"],
    }
    for k, v in meta_over.items():
        meta[k] = v if isinstance(v, list) else [v]
    return {"key": "fleet-jobs-report", "lang": "en",
            "meta": meta, "contentMd": md}


class _FakeMddb:
    def __init__(self, doc):
        self.doc = doc
        self.calls: list[tuple] = []

    async def get_document(self, collection, key, lang="en", **kw):
        self.calls.append((collection, key, lang))
        return self.doc


def _runner(mddb):
    return SimpleNamespace(context=SimpleNamespace(mddb=mddb))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_declaration_is_slim_and_focus_optional():
    m = _load()
    assert m.DECLARATION["name"] == "ada_fleet_jobs"
    assert len(m.DECLARATION["description"]) <= 300
    assert "focus" in m.DECLARATION["parameters"]["properties"]
    assert "required" not in m.DECLARATION["parameters"]


def test_reads_exact_contract_target():
    m = _load()
    mddb = _FakeMddb(_doc(_MD, _now() - timedelta(hours=1)))
    out = asyncio.run(m.run(_runner(mddb)))
    assert out["ok"] is True
    # the ONLY read: POST /get on ada-cms-pages/fleet-jobs-report en
    assert mddb.calls == [("ada-cms-pages", "fleet-jobs-report", "en")]


def test_fresh_overview_answers_from_page():
    m = _load()
    mddb = _FakeMddb(_doc(_MD, _now() - timedelta(hours=1)))
    out = asyncio.run(m.run(_runner(mddb)))
    assert out["ok"] is True and out["mode"] == "overview"
    assert out["freshness"]["state"] == "fresh"
    answer = out["answer"]
    # meta.summary leads, then the parsed health + spend sentences
    assert "11 claimable/7 in-flight" in answer
    assert "14 timers failing across 2 hosts" in answer
    assert "ada-ocb-smoke (idc02)" in answer
    assert "$1.68" in answer
    assert out["stats"]["failing_timers"] == 14
    assert out["report"]["key"] == "fleet-jobs-report"


def test_stale_report_prefixes_as_of_and_flags_producer():
    m = _load()
    updated = _now() - timedelta(hours=5)
    mddb = _FakeMddb(_doc(_MD, updated))
    out = asyncio.run(m.run(_runner(mddb)))
    assert out["ok"] is True
    assert out["freshness"]["state"] == "stale"
    assert out["freshness"]["age_s"] > 4 * 3600
    assert out["answer"].startswith("As of 20")
    assert "producer may be down" in out["answer"]
    # same body underneath the prefix — the page is still the source
    assert "11 claimable/7 in-flight" in out["answer"]


def test_missing_page_says_not_generated_and_offers_card():
    m = _load()
    mddb = _FakeMddb(None)
    out = asyncio.run(m.run(_runner(mddb)))
    assert out["ok"] is True and out["generated"] is False
    assert "hasn't been generated" in out["answer"]
    assert "card" in out["answer"]
    assert out["freshness"]["state"] == "missing"


def test_unreachable_store_reads_as_missing():
    m = _load()

    class _Down:
        async def get_document(self, *a, **kw):
            raise RuntimeError("connection refused")

    out = asyncio.run(m.run(_runner(_Down())))
    assert out["ok"] is True and out["generated"] is False
    assert "hasn't been generated" in out["answer"]


def test_no_mddb_is_honest_failure():
    m = _load()
    out = asyncio.run(m.run(SimpleNamespace(context=None)))
    assert out["ok"] is False
    assert "memory store" in out["error"]


def test_empty_page_content_is_not_fabricated():
    m = _load()
    doc = _doc("", _now())
    doc["contentMd"] = "   \n"
    out = asyncio.run(m.run(_runner(_FakeMddb(doc))))
    assert out["ok"] is True and out["generated"] is False


def test_unparseable_updated_marks_unverified():
    m = _load()
    doc = _doc(_MD, _now())
    doc["meta"]["updated"] = ["not-a-timestamp"]
    out = asyncio.run(m.run(_runner(_FakeMddb(doc))))
    assert out["ok"] is True
    assert out["freshness"]["state"] == "unknown"
    assert "unverified" in out["answer"]


def test_focus_spend():
    m = _load()
    mddb = _FakeMddb(_doc(_MD, _now() - timedelta(minutes=30)))
    out = asyncio.run(m.run(_runner(mddb), focus="how much did we spend"))
    assert out["ok"] is True and out["mode"] == "spend"
    assert "$1.68" in out["answer"] and "14 calls" in out["answer"]
    assert "gpt-4o" in out["answer"]


def test_focus_timers_names_hosts():
    m = _load()
    mddb = _FakeMddb(_doc(_MD, _now() - timedelta(minutes=30)))
    out = asyncio.run(m.run(_runner(mddb), focus="timers"))
    assert out["mode"] == "timers"
    assert "idc02" in out["answer"] and "idc03" in out["answer"]
    assert "mddb-backup" in out["answer"]


def test_focus_lanes_and_queue():
    m = _load()
    mddb = _FakeMddb(_doc(_MD, _now() - timedelta(minutes=30)))
    lanes = asyncio.run(m.run(_runner(mddb), focus="lanes"))
    assert lanes["mode"] == "lanes"
    assert "devin (4 running, 4 queued)" in lanes["answer"]
    queue = asyncio.run(m.run(_runner(mddb), focus="queue"))
    assert queue["mode"] == "queue"
    assert "11 claimable" in queue["answer"]
    assert "7 in-flight" in queue["answer"]


def test_legacy_page_generation_still_parses():
    m = _load()
    mddb = _FakeMddb(_doc(_MD_LEGACY, _now() - timedelta(hours=1),
                          summary=["legacy summary"]))
    out = asyncio.run(m.run(_runner(mddb)))
    assert out["ok"] is True
    assert out["freshness"]["state"] == "fresh"
    # '## Live Queue' counts + emoji 🔴 failing + Attention timers names
    assert "Legacy summary" in out["answer"]
    assert out["stats"]["claimable"] == 11
    assert out["stats"]["in_flight"] == 7
    assert "2 timers failing across 1 host" in out["answer"]
    assert "ada-ocb-smoke (idc02)" in out["answer"]


def test_no_spend_data_says_so():
    m = _load()
    md = _MD.split("### LiteLLM Spend")[0] + "_(No spend data available)_\n"
    doc = _doc(md, _now() - timedelta(minutes=30))
    out = asyncio.run(m.run(_runner(_FakeMddb(doc)), focus="spend"))
    assert out["mode"] == "spend"
    assert "no LiteLLM spend data" in out["answer"]
