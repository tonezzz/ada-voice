"""gesture_controls — ada-pi desk-companion gesture-control status/toggle.

The desk companion (backend/main.py on the ada-pi host) runs a YOLO-pose
gesture controller on its shared camera stream: a wave arms the
controller, swipes navigate the kiosk panels, a held palm opens the
settings drawer, and dropping the arm dismisses. This tool reports and
toggles that feature through the companion's REST API
(GET/PATCH /api/gestures).

ADA_PI_URL sets the companion base URL (default http://idc02:8000 — the
ada-pi deployment host). Honest-empty contract: when the companion is
unreachable the tool says so plainly instead of guessing the state.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://idc02:8000"
TIMEOUT = 10.0

DECLARATION = {
    "name": "gesture_controls",
    "description": (
        "Desk-companion gesture controls (YOLO pose — wave arms, swipe "
        "navigates panels, palm-hold opens settings, drop dismisses). "
        "op='status'|'enable'|'disable'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["status", "enable", "disable"]},
            "confirmed": {"type": "boolean"},
            "confirm_token": {"type": "string"},
        },
    },
}


def _base_url() -> str:
    return os.environ.get("ADA_PI_URL", DEFAULT_BASE_URL).rstrip("/")


async def run(runner: Any, confirmed: Any = None,
              confirm_token: Any = None, **args: Any) -> dict[str, Any]:
    op = str(args.get("op") or "status").strip().lower()
    if op not in {"status", "enable", "disable"}:
        return {"ok": False, "error": "op must be status, enable, or disable"}
    if op != "status":
        # Toggling input control on the desk companion is a Tony-decides
        # change — the standard runner confirm gate (needs_confirm dict on
        # denial, confirmed=true or bound token on the retry).
        try:
            runner._require_confirmation(
                "gesture_controls",
                {"op": op},
                confirmed, confirm_token,
                f"NOT EXECUTED — gesture_controls {op}: this changes how "
                "the desk companion responds to hand gestures. Say what "
                "it will do and get Tony's explicit yes first.")
        except PermissionError as exc:
            return {"ok": False, "needs_confirm": True,
                    "error": str(exc)}
    base_url = _base_url()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            if op == "status":
                resp = await client.get(f"{base_url}/api/gestures")
            else:
                resp = await client.patch(
                    f"{base_url}/api/gestures",
                    json={"enabled": op == "enable"},
                )
    except (httpx.HTTPError, TimeoutError) as exc:
        return {
            "ok": False,
            "error": (
                f"the desk companion is unreachable "
                f"({exc.__class__.__name__}) — check that ada-pi is "
                f"running at {base_url}"),
        }
    if resp.status_code >= 400:
        return {
            "ok": False,
            "error": f"the desk companion returned HTTP {resp.status_code}",
        }
    try:
        status = resp.json()
    except ValueError:
        return {"ok": False,
                "error": "the desk companion returned an unreadable body"}
    if not isinstance(status, dict):
        return {"ok": False,
                "error": "the desk companion returned an unexpected body"}
    enabled = bool(status.get("enabled"))
    last_event = status.get("last_event") or {}
    event_name = last_event.get("name")
    summary = (
        f"gesture controls are {'on' if enabled else 'off'} — "
        f"controller {status.get('state', 'unknown')}"
        + (f", last gesture {event_name}" if event_name else "")
    )
    return {
        "ok": True,
        "op": op,
        "enabled": enabled,
        "state": status.get("state"),
        "armed_remaining_s": status.get("armed_remaining_s"),
        "last_event": status.get("last_event"),
        "companion": base_url,
        "summary": summary,
    }
