"""host_status tools.d adapter — no network (card ada-status-live-state-preference)."""
import asyncio
import importlib.util
from pathlib import Path

MOD = Path(__file__).resolve().parents[1] / "backend" / "tools.d" / "host_status.py"


def _load():
    spec = importlib.util.spec_from_file_location("host_status_tool", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _Resp:
    def __init__(self, code, payload=None, text=""):
        self.status_code, self._p, self.text = code, payload, text

    def json(self):
        return self._p


def _client(resp=None, exc=None, seen=None):
    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            if seen is not None:
                seen.append(url)
            if exc:
                raise exc
            return resp
    return C


def test_declaration_is_slim_and_requires_host():
    m = _load()
    assert m.DECLARATION["name"] == "host_status"
    assert len(m.DECLARATION["description"]) <= 300
    assert m.DECLARATION["parameters"]["required"] == ["host"]


def test_missing_host_is_an_error_without_a_call():
    m = _load()
    out = asyncio.run(m.run(None))
    assert out == {"ok": False, "error": "host is required"}


def test_success_returns_server_payload_and_quotes_host(monkeypatch):
    m = _load()
    seen = []
    payload = {"ok": True, "host": "kk-mac", "confidence": "live",
               "conflicts": [], "answer": "kk-mac is ONLINE"}
    monkeypatch.setattr(m.httpx, "AsyncClient",
                        _client(_Resp(200, payload), seen=seen))
    out = asyncio.run(m.run(None, host="KKs Mac Studio"))
    assert out == payload
    assert seen == [f"{m.BASE}/host-status?host=KKs%20Mac%20Studio"]


def test_non_200_reports_status_not_a_crash(monkeypatch):
    m = _load()
    monkeypatch.setattr(m.httpx, "AsyncClient",
                        _client(_Resp(404, text="not found")))
    out = asyncio.run(m.run(None, host="idc03"))
    assert out["ok"] is False and "404" in out["error"]


def test_network_failure_fails_soft(monkeypatch):
    m = _load()
    monkeypatch.setattr(m.httpx, "AsyncClient",
                        _client(exc=RuntimeError("tailnet down")))
    out = asyncio.run(m.run(None, host="idc03"))
    assert out["ok"] is False and "tailnet down" in out["error"]
