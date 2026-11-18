"""Unit tests for opencode/ada_mcp.py board snapshots + card resolution.

Covers the fast board-read path (ff2d6a1): find_card resolves
compact -> projected -> hydrate, _get_json_cached honours the 20s
snapshot TTL, and string comms/requests/action rows from board-api
are tolerated instead of crashing resolution.
"""
import importlib.util
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
