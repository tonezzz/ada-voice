"""weather_station — current readings from the RK600 station at
Michael's site (real sensor, ~1-min cadence via RikaCloud).

Answers "how windy is it at Michael's / what's the weather at the site"
with the station's own numbers — wind speed (knots) + direction, air
temperature, humidity, rain intensity — plus the data age so the model
narrates staleness honestly instead of presenting old data as current.

Env (rikacloud.env on the backend host):
    RIKA_ACCOUNT, RIKA_PASSWORD

The JWT rides the `token` header (not Authorization); TTL is 86400s and
cached under ~/.local/state/ada/rika-token.txt.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

API = os.environ.get("ADA_RIKA_API", "http://39.99.253.232:8005").rstrip("/")
FACILITY = os.environ.get("ADA_RIKA_FACILITY", "22100502")
ENV_FILE = os.environ.get(
    "ADA_RIKA_ENV", os.path.expanduser("~/.config/secrets/rikacloud.env"))
TOKEN_CACHE = Path(os.path.expanduser("~/.local/state/ada/rika-token.txt"))
# e2 is cm/s (Oct-14 storm peaked 1129 ~ 22kt) -> knots = e2/100*1.94384
FRESH_S = 300

_DIRS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
         "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def _load_env(path: str = ENV_FILE) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except OSError:
        pass
    return env


def _compass(deg: float) -> str:
    return _DIRS[int((deg % 360) / 22.5 + 0.5) % 16]


async def _login(client: httpx.AsyncClient, env: dict[str, str]) -> str:
    r = await client.post(f"{API}/login", json={
        "username": env["RIKA_ACCOUNT"], "password": env["RIKA_PASSWORD"]})
    r.raise_for_status()
    tok = r.json()["token"]
    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE.write_text(tok)
    return tok


async def _latest(client: httpx.AsyncClient, env: dict[str, str]) -> dict:
    tok = TOKEN_CACHE.read_text().strip() if TOKEN_CACHE.exists() else ""
    for attempt in range(2):
        if not tok:
            tok = await _login(client, env)
        r = await client.get(f"{API}/data/{FACILITY}",
                             headers={"token": tok})
        if r.status_code in (401, 403) and attempt == 0:
            tok = ""
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("unreachable")


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    env = _load_env()
    if not env.get("RIKA_ACCOUNT"):
        return {"ok": False,
                "error": "rikacloud.env missing on this host "
                         "(RIKA_ACCOUNT unset)"}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            row = await _latest(client, env)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    dt = datetime.strptime(row["dataTime"], "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone(timedelta(hours=8)))  # dataTime is +08 server time
    age_s = max(0, int(time.time() - dt.timestamp()))
    deg = float(row.get("e1") or 0)
    wind_kt = round((row.get("e2") or 0) / 100 * 1.94384, 1)
    out: dict[str, Any] = {
        "ok": True,
        "source": "RK600 station at Michael's site (RikaCloud)",
        "data_time": row["dataTime"],
        "age_min": round(age_s / 60, 1),
        "stale": age_s > FRESH_S,
        "wind_kt": wind_kt,
        "wind_dir_deg": deg,
        "wind_dir": _compass(deg),
        "temp_c": round((row.get("e3") or 0) / 10, 1),
        "humidity_pct": round((row.get("e4") or 0) / 10, 1),
        "rain": row.get("e5") or 0,
        "raining": bool(row.get("e5")),
    }
    if out["stale"]:
        out["note"] = ("Station data is stale — narrate the age honestly "
                       "(the upload may be down; do not present as "
                       "current conditions).")
    return out


DECLARATION = {
    "name": "weather_station",
    "description": (
        "Live readings from the RK600 station at Michael's site: wind "
        "kt+dir, temp, humidity, rain. Narrate stale data honestly."),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}
