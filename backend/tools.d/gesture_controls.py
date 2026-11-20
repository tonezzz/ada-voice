"""gesture_controls — drop-in tool (card OPS-0683).

Voice control for the desk companion's YOLO-pose gesture navigation
(backend/gesture_controls.py): wave arms the controller, swipe cycles
kiosk panels, palm-hold opens settings, drop dismisses. Actions:
status (state, counters, last gesture), enable, disable.
"""

from __future__ import annotations

from typing import Any

DECLARATION = {
    "name": "gesture_controls",
    "description": (
        "Desk-companion hand-gesture control (YOLO-pose wrist tracking). "
        "action: status | enable | disable. Wave arms it; swipe navigates "
        "panels, palm-hold opens settings, drop dismisses."
    ),
    "parameters": {
        "type": "object",
        "properties": {"action": {"type": "string",
                                  "enum": ["status", "enable", "disable"]}},
        "required": ["action"],
    },
}


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    from backend.gesture_controls import active_service
    service = active_service()
    if service is None:
        return {"ok": False,
                "error": "gesture control service is not running on this "
                         "instance — no camera/pose pipeline here"}
    action = str(args.get("action") or "status").strip().lower()
    if action == "status":
        return {"ok": True, **service.status()}
    if action in {"enable", "disable"}:
        settings = service.update_settings({"enabled": action == "enable"})
        return {"ok": True, "state": service.state(), "settings": settings}
    return {"ok": False,
            "error": f"unknown action {action!r} — use status|enable|disable"}
