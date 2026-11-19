"""Unit tests for opencode/ada_mcp.py board snapshots + card resolution.

Covers the fast board-read path (ff2d6a1): find_card resolves
compact -> projected -> hydrate, _get_json_cached honours the 20s
snapshot TTL, and string comms/requests/action rows from board-api
are tolerated instead of crashing resolution.
"""
import importlib.util
import json
import os
import tempfile
import time
import types
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="mcp package (or tests dep stub) required")

ADA_MCP = Path(__file__).resolve().parents[1] / "opencode" / "ada_mcp.py"
_AUDIT = tempfile.mkdtemp(prefix="ada-mcp-snap-audit-")


def _load():
    os.environ["ADA_CONTROL_AUDIT_DIR"] = _AUDIT
    spec = importlib.util.spec_from_file_location(
        "ada_mcp_snapshots_under_test", ADA_MCP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COMPACT_PATH = "/cards?compact=1&limit=1000"


def _compact_row(card_id, ref, title, column="backlog", action_status=None):
    return {
        "id": card_id, "ref": ref, "title": title,
        "column": column, "updated": "2026-11-18 08:00",
        "action_status": action_status,
    }


def _full_row(card_id, ref, title, **extra):
    row = _compact_row(card_id, ref, title)
    row.update({
        "brief": f"brief for {ref}",
        "spec": f"spec for {ref}",
        "priority": "medium",
        "action": {},
        "requests": [],
        "comms": [{"at": "2026-11-18 08:00", "from": "devin", "text": "hi"}],
    })
    row.update(extra)
    return row


def _patch_board(monkeypatch, mod, compact=None, projected=None, single=None):
    """Route _get_json by snapshot shape; record the path sequence."""
    calls = []

    def fake(path, timeout=15):
        calls.append(path)
        if "compact=1" in path:
            return {"cards": list(compact or [])}
        if "id=" in path:
            rows = single(path) if callable(single) else (single or [])
            return {"cards": list(rows)}
        return {"cards": list(projected or [])}

    monkeypatch.setattr(mod, "_get_json", fake)
    monkeypatch.setattr(mod, "_get_json_cached", lambda path, ttl=20.0: fake(path))
    return calls


@pytest.fixture()
def ada_mcp():
    mod = _load()
    mod._snapshot_cache.clear()
    mod.PENDING_NEXT_SELECTIONS.clear()
    return mod


def test_snapshot_ttl_constant_is_20_seconds(ada_mcp):
    assert ada_mcp._SNAPSHOT_TTL == 20.0


def test_cached_get_reuses_snapshot_within_ttl(ada_mcp, monkeypatch):
    fetched = []
    monkeypatch.setattr(
        ada_mcp, "_get_json",
        lambda path, timeout=15: fetched.append(path) or {"ok": True})
    clock = [1000.0]
    monkeypatch.setattr(ada_mcp, "time", types.SimpleNamespace(
        monotonic=lambda: clock[0], time=time.time, strftime=time.strftime))
    assert ada_mcp._get_json_cached("/cards?compact=1&limit=1000") == {"ok": True}
    clock[0] += 19.9
    assert ada_mcp._get_json_cached("/cards?compact=1&limit=1000") == {"ok": True}
    assert fetched == ["/cards?compact=1&limit=1000"]


def test_cached_get_refetches_after_ttl_and_evicts(ada_mcp, monkeypatch):
    fetched = []
    monkeypatch.setattr(
        ada_mcp, "_get_json",
        lambda path, timeout=15: fetched.append(path) or {"n": len(fetched)})
    clock = [500.0]
    monkeypatch.setattr(ada_mcp, "time", types.SimpleNamespace(
        monotonic=lambda: clock[0], time=time.time, strftime=time.strftime))
    ada_mcp._get_json_cached("/a")
    ada_mcp._get_json_cached("/b")
    clock[0] += 21.0  # past the 20s snapshot TTL
    ada_mcp._get_json_cached("/c")  # miss -> fetch -> evicts /a and /b
    assert fetched == ["/a", "/b", "/c"]
    assert list(ada_mcp._snapshot_cache) == ["/c"]
    assert ada_mcp._get_json_cached("/a") == {"n": 4}  # /a expired -> refetch
    assert fetched[-1] == "/a"


def test_compact_hit_resolves_without_projected_snapshot(ada_mcp, monkeypatch):
    compact = [_compact_row("ada-ocb-board-read-latency", "ADA-0172", "fast board reads")]
    hydrate = _full_row("ada-ocb-board-read-latency", "ADA-0172", "fast board reads")
    calls = _patch_board(
        monkeypatch, ada_mcp, compact=compact,
        projected=[_compact_row("other", "ADA-0001", "unrelated")],
        single=lambda path: [hydrate],
    )
    card = ada_mcp.find_card("ADA-0172")
    assert card["id"] == "ada-ocb-board-read-latency"
    assert card["comms"]  # hydrated row, not the compact index row
    assert calls[0] == COMPACT_PATH
    assert all("compact=1" not in p for p in calls[1:])
    assert all("id=ada-ocb-board-read-latency" in p for p in calls[1:])
    assert len(calls) == 2  # compact + single-card hydrate only
    # hydration asks board-api for a bounded comms tail (KNB-0176)
    assert f"comms_tail={ada_mcp._HYDRATE_COMMS_TAIL}" in calls[1]


def test_projected_snapshot_used_for_alias_title_token(ada_mcp, monkeypatch):
    projected = [
        _full_row("card-x", "ADA-0200", "windowed ssot excerpts",
                  aliases=["excerpt-card"]),
        _full_row("card-y", "ADA-0201", "unrelated work"),
    ]
    calls = _patch_board(monkeypatch, ada_mcp, compact=[_compact_row(
        "other", "ADA-0001", "unrelated")], projected=projected,
        single=lambda path: [projected[0]])
    # alias match lives only on the projected snapshot
    card = ada_mcp.find_card("excerpt-card")
    assert card["id"] == "card-x"
    assert calls[0] == COMPACT_PATH
    assert any("fields=" in p and "id=" not in p for p in calls)
    assert calls.index(next(p for p in calls if "fields=" in p and "id=" not in p)) \
        < calls.index(next(p for p in calls if "id=" in p))
    # title-substring path also goes through the projected snapshot
    calls.clear()
    card = ada_mcp.find_card("windowed ssot")
    assert card["id"] == "card-x"


def test_ambiguous_compact_match_raises_without_more_reads(ada_mcp, monkeypatch):
    compact = [
        _compact_row("dup-a", "ADA-0555", "first"),
        _compact_row("dup-b", "ADA-0555", "second"),
    ]
    calls = _patch_board(monkeypatch, ada_mcp, compact=compact)
    with pytest.raises(LookupError, match="matched 2 cards"):
        ada_mcp.find_card("ADA-0555")
    assert calls == [COMPACT_PATH]


def test_kanban_sweep_classifies_attention_read_only(ada_mcp, monkeypatch):
    queue = {
        "queued": [
            {
                "id": "manual-card", "ref": "ADA-0501", "title": "Manual item",
                "action": {"type": "manual"},
                "blocked": {"idc03": "unknown type 'manual'"},
            },
            {
                "id": "blocked-card", "ref": "ADA-0502", "title": "Blocked item",
                "action": {"type": "dispatch"}, "eligible": [],
                "blocked": {"idc02": "host overloaded"},
            },
            {
                "id": "runnable-card", "ref": "ADA-0503", "title": "Runnable item",
                "action": {"type": "dispatch"}, "eligible": ["idc02"],
                "blocked": {},
            },
        ],
        "claimed": [{
            "id": "live-card", "ref": "ADA-0504", "title": "Live item",
            "status": "running", "host": "idc02",
            "unit": "devin-task-live.service", "at": 100,
        }],
        "hosts": {
            "idc02": {"stale": False, "active": [{"id": "live-card"}]},
            "idc03": {"stale": True, "active": []},
        },
    }
    cards = [
        _full_row(
            "failed-admission", "ADA-0510", "Admission failure",
            column="doing", updated="2026-11-19 07:30",
            action={
                "type": "dispatch", "status": "failed",
                "result": "start failed: OpenCode admission requires a complete action.lane_envelope",
            },
        ),
        _full_row(
            "failed-runner", "ADA-0511", "Runner failure",
            column="doing", updated="2026-11-18 07:00",
            action={
                "type": "dispatch", "status": "failed", "result": "exit=1",
                "sweep": {"failed_noted": "2026-11-18 08:00"},
            },
        ),
        _full_row(
            "orphan-running", "ADA-0512", "Missing active unit",
            column="doing", updated="2026-11-18 07:00",
            action={"type": "dispatch", "status": "running"},
        ),
        _full_row(
            "done-in-doing", "ADA-0513", "Done but still doing",
            column="doing", updated="2026-11-19 05:00",
            action={"type": "dispatch", "status": "done"},
        ),
        _full_row(
            "idle-stale", "ADA-0514", "Stale idle work",
            column="doing", updated="2026-11-17 07:00",
            requests=[{"id": "old-question", "status": "open"}],
        ),
        _full_row(
            "awaiting-human", "ADA-0515", "Open decision",
            column="doing", updated="2026-11-19 07:00",
            requests=[{"id": "decision", "status": "pending"}],
        ),
        _full_row(
            "live-card", "ADA-0504", "Live item", column="doing",
            updated="2026-11-19 08:00",
            action={"type": "dispatch", "status": "running", "runner": "idc02"},
        ),
        "legacy string row",
    ]
    calls = []

    def fake_get(path, ttl=20.0):
        calls.append(path)
        return queue if path == "/queue" else {"ok": True, "cards": cards}

    monkeypatch.setattr(ada_mcp, "_get_json_cached", fake_get)
    monkeypatch.setattr(
        ada_mcp, "_post_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("sweep must not mutate the board")),
    )
    now = ada_mcp.datetime.strptime(
        "2026-11-19 08:00", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=ada_mcp.ZoneInfo("Asia/Bangkok"))
    monkeypatch.setattr(ada_mcp, "_sweep_now", lambda: now)

    doc = json.loads(ada_mcp.ada_kanban_sweep(window_hours=24, limit=20))

    assert doc["ok"] is True
    assert doc["mutations_performed"] == []
    assert doc["queue"] == {
        "queued": 3, "claimed": 1, "fresh_hosts": 1, "stale_hosts": 1,
    }
    assert [item["id"] for item in doc["active"]] == ["live-card"]
    assert doc["active_count"] == 1
    assert doc["source_truncated"] is False
    items = {item["id"]: item for item in doc["items"]}
    assert items["manual-card"]["category"] == "manual_only"
    assert items["blocked-card"]["category"] == "queued_blocked"
    assert items["failed-admission"]["category"] == "failed_admission_blocked"
    assert items["failed-runner"]["failure_class"] == "runner_failed"
    assert items["failed-runner"]["time_bucket"] == "stale"
    assert items["orphan-running"]["category"] == "active_missing_from_queue"
    assert items["done-in-doing"]["category"] == "done_still_in_doing"
    assert items["idle-stale"]["category"] == "stale_doing_without_active_action"
    assert items["idle-stale"]["open_request_count"] == 1
    assert "do not re-queue until answered" in items["idle-stale"]["next_safe_step"]
    assert items["awaiting-human"]["category"] == "awaiting_human"
    assert doc["active"][0]["category"] == "active_in_queue"
    assert "runnable-card" not in items
    assert calls[0] == "/queue"
    assert "column=doing" in calls[1]
    assert "fields=" in calls[1]


def test_kanban_sweep_fails_closed_on_invalid_queue(ada_mcp, monkeypatch):
    calls = []
    monkeypatch.setattr(
        ada_mcp, "_get_json_cached",
        lambda path, ttl=20.0: calls.append(path) or {"queued": [], "claimed": []},
    )
    doc = json.loads(ada_mcp.ada_kanban_sweep())

    assert doc["ok"] is False
    assert "invalid shape" in doc["error"]
    assert doc["mutations_performed"] == []
    assert calls == ["/queue"]


def test_unmatched_reference_raises_lookup(ada_mcp, monkeypatch):
    _patch_board(monkeypatch, ada_mcp,
                 compact=[_compact_row("a", "ADA-0001", "alpha")],
                 projected=[_full_row("a", "ADA-0001", "alpha")])
    with pytest.raises(LookupError, match="no board card matched"):
        ada_mcp.find_card("ZZZ-9999 nonexistent")


def test_second_find_card_served_from_snapshot_cache(ada_mcp, monkeypatch):
    compact = [_compact_row("c1", "ADA-0172", "fast board reads")]
    hydrate = _full_row("c1", "ADA-0172", "fast board reads")
    raw_calls = []

    def fake(path, timeout=15):
        raw_calls.append(path)
        if "compact=1" in path:
            return {"cards": list(compact)}
        return {"cards": [hydrate]}

    monkeypatch.setattr(ada_mcp, "_get_json", fake)
    first = ada_mcp.find_card("ADA-0172")
    second = ada_mcp.find_card("c1")
    assert first["id"] == second["id"] == "c1"
    # both compact and the hydrate row come from _get_json_cached —
    # the second resolution issues zero network reads.
    assert len(raw_calls) == 2


def test_string_comms_requests_action_rows_tolerated(ada_mcp, monkeypatch):
    """board-api rows with string comms/requests/action must not crash."""
    compact = [_compact_row("legacy", "ADA-0300", "legacy string rows")]
    hydrate = _full_row(
        "legacy", "ADA-0300", "legacy string rows",
        comms=["plain comm line", {"at": "2026-11-18", "from": "x", "text": "dict row"}],
        requests=["an open string request"],
        action="manual",
    )
    _patch_board(monkeypatch, ada_mcp, compact=compact,
                 single=lambda path: [hydrate])
    safe = ada_mcp._safe_card(ada_mcp.find_card("ADA-0300"))
    assert safe["action"] == {}           # string action -> {}
    assert safe["requests"] == ["an open string request"]
    assert len(safe["comms_tail"]) == 2   # string comm rows preserved
    # a string request row counts as open -> the card is not actionable
    assert ada_mcp._card_actionable(hydrate) is False
    # string comm/request rows still flow into the context text
    text = ada_mcp._context_card_text(hydrate)
    assert "plain comm line" in text and "an open string request" in text


def test_cards_payload_drops_nondict_rows(ada_mcp):
    payload = {"cards": [{"id": "ok"}, "stray", 42, None, {"id": "ok2"}]}
    assert [c["id"] for c in ada_mcp._cards_payload(payload)] == ["ok", "ok2"]
    assert ada_mcp._cards_payload({"unexpected": True}) == []
    assert ada_mcp._cards_payload([1, 2, 3]) == []


def test_pick_cards_tolerates_string_rows(ada_mcp, monkeypatch):
    cards = [
        _full_row("stale-doing", "ADA-0401", "idle work",
                  column="doing", updated="2026-10-01 08:00"),
        _full_row("string-req", "ADA-0402", "waiting on a string request",
                  column="backlog", requests=["unanswered ask"]),
        _full_row("string-action", "ADA-0403", "claimed via string row",
                  column="backlog", action="claimed"),
        _full_row("fresh-backlog", "ADA-0404", "actionable backlog",
                  column="backlog"),
    ]
    monkeypatch.setattr(ada_mcp, "_board_cards", lambda: cards)
    picks = ada_mcp._pick_cards("sweep", 7, 10)
    ids = [p["card"]["id"] for p in picks]
    assert "string-req" not in ids      # string request row counts as open
    # string action decodes to {} -> no status -> still actionable/picked
    assert "string-action" in ids
    assert "stale-doing" in ids
    assert "fresh-backlog" in ids
    # stale strategy only surfaces doing cards idle >= days
    stale = ada_mcp._pick_cards("stale", 7, 10)
    assert [p["card"]["id"] for p in stale] == ["stale-doing"]
