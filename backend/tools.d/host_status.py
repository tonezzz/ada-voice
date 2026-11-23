"""host_status — live-first answer for "what is <host> doing / is
<host> up".

Thin adapter over the chaba board-api read door (GET
/host-status?host=) — the server composes live telemetry (ada-nest
heartbeats, the kk_mac lane admission probe, nest-mgr nest-<host> pages,
runner claims) BEFORE board records, tags every board-derived claim
with its record age, and flags telemetry/board contradictions.

Why this tool exists (OPS-1045, card ada-status-live-state-preference):
a "what is kk-mac processing" ask was answered from a card comm hours
stale — "offline 22h, 15GiB free" while the host was online and
eligible with ~92-115GiB free. When the question is about a host's
CURRENT state (health, disk, what it's running), call this tool and
answer from `live`/`answer`/`conflicts` — never quote card comms as
current state. When `conflicts` is non-empty, name both sides.

Env:
    ADA_HOST_STATUS_API — board-api base
        (default https://idc03.taila0626a.ts.net:10000)
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Any

import httpx

BASE = os.environ.get(
    "ADA_HOST_STATUS_API", "https://idc03.taila0626a.ts.net:10000"
).rstrip("/")

DECLARATION = {
    "name": "host_status",
    "description": (
        "Live status of one fleet host (kk-mac, tony-omen, idc03, ...): "
        "online, disk free, what it is processing. Live first; see guide."),
    "parameters": {
        "type": "object",
        "properties": {
            "host": {
                "type": "string",
                "description": "Host name or alias — 'kk-mac', "
                               "'KKs-Mac-Studio', 'tony-omen', 'idc03' "
                               "all resolve.",
            },
        },
        "required": ["host"],
    },
}


async def run(runner: Any, **args: Any) -> dict:
    host = str(args.get("host") or "").strip()
    if not host:
        return {"ok": False, "error": "host is required"}
    url = f"{BASE}/host-status?host={urllib.parse.quote(host)}"
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.get(url)
        if r.status_code != 200:
            return {"ok": False,
                    "error": f"host-status {r.status_code}: "
                             f"{r.text[:160]}"}
        return r.json()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
