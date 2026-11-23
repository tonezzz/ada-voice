# Dispatch outcome — runner-agent drift (tony-dell) + gesture-controls salvage

Card: rebuild-gesture-controls-using-yolo-190356 (OPS-0683) · attempt 2
Branch: dispatch/20261123-220744-deploy-the-updated-runner-agen
Runner: tony-omen

## What happened

Two-part spec: (1) deploy the updated chaba repo
`scripts/board/runner-agent.py` to tony-dell to clear the drift defers
that were starving the claim loop on every host; (2) rebuild gesture
controls on the desk companion over the shared YOLO pose stream.

**runner-agent (tony-dell):** the drift was already cleared before this
run — `~/.local/bin/runner-agent` on tony-dell (mtime 2026-11-23 19:10)
was already byte-identical to the chaba repo copy (sha1sum
`8d09af1428cf…`, git blob `babd0eaa50…`, = origin/master). Ran the
canonical deploy anyway — `scp` repo copy + `install -m 0755` — and
re-verified identical on both ends. Spot-checked the fleet: idc02,
idc03, mn01 all clean; tony-macbook ssh refused connection (unchecked,
out of scope).

**gesture controls:** the verified deliverable already existed —
commit `5307864b` on tony-dell's local branch
`dispatch/20261120-215246-rebuild-gesture-controls-using` (never pushed,
never merged; two prior dispatches exited nonzero after finishing the
work). Fetched it over ssh and cherry-picked onto current
`origin/main` (fcb38ee) as `d49f7d2`. Conflicts: `tool_runner/__init__.py`
resolved to HEAD (the `dyn_spec.run` confirmed/confirm_token fix it
carried is already upstream — landed diff is gesture-files only);
`ssot.tool-surface.yml` count_cap comment merged, cap stays 115.

The landed code: `backend/gesture_controls.py` (GestureController state
machine + GestureService riding `pose_service.results()` — Hailo
yolov8s_pose / MoveNet fallback, no second camera loop), wired in
`backend/main.py` (GET/PATCH `/api/gestures`, `/ws/gesture` fan-out,
start/stop in lifespan), `tools.d/gesture_controls` (status|enable|
disable, confirm-gated), kiosk UI chip/toggle/handlers, 16 unit tests
+ frontend contract assertions + offline + live scenarios.

## Result

- `pytest tests/test_gesture_controls.py tests/test_frontend_contract.py`: **33 passed**
- `pytest tests/ -x -q` (deploy gate): **1927 passed, 4 skipped** (3m03s)
- `scripts/tool-lint.py`: **clean** (73 declared, cap 115)
- `scripts/scenario-schema-check.py`: **209 files, 0 violations**
- runner-agent on tony-dell: sha1sum/git-blob identical to chaba repo after deploy
- runner-agent.service on tony-dell: activating/running via timer, no restart needed (bytes unchanged)

## How to verify

```
# gesture code on this branch
git show d49f7d2 --stat
backend/gesture_controls.py: GestureService._run() -> pose_service.results()
backend/main.py: @app.get("/api/gestures") @app.patch("/api/gestures") @app.websocket("/ws/gesture")

# gates (venv python from main checkout works)
/home/tony/CascadeProjects/ada-voice/.venv/bin/python -m pytest tests/test_gesture_controls.py -q
python3 scripts/tool-lint.py

# runner-agent drift cleared
ssh tony-dell 'sha1sum ~/.local/bin/runner-agent'   # 8d09af1428cffee09af46bbbbf8e6b9e3d3a1093
```

Live-surface check (real `/ws/gesture` stream driving kiosk panels on the
desk companion) still needs the normal merge → `deploy-ada.sh` flow on
idc03 — this dispatch lands verified code only, per dispatch rules.

lessons:
- Dispatch branches for this card keep dying exit=1 AFTER posting "DONE" comms — the work was complete twice; salvage the branch tip over ssh (`git fetch ssh://<host>/<repo> <branch>`) instead of rebuilding.
- `git fetch origin main` fails in chaba checkouts — default branch is `master`, not `main`.
- runner-agent drift self-heals between defers (fleet sync jobs); verify sha before redeploying — the deploy is idempotent anyway.
- tools.d `confirmed`/`confirm_token` envelope fix was already upstream; cherry-pick conflicts there resolve to HEAD.
