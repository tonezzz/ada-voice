# dispatch-outcome — ada-benchmark-coverage-scenarios-for-kan-152a4f (ADA-1204)

## Result

Done. Commit `a77e51e` on `dispatch/20261119-171510-ada-benchmark-coverage-scenari`.
11 new offline scenarios + scenario-harness fixtures + one real
tool_runner bug fix. Not pushed, not merged, not deployed.

## What changed

### Harness — `tests/scenario_engine.py`

- `FakeBoard` + `board:` fixture — in-memory board-api stand-in patched
  at the `board_client.request` seam so real tool code runs verbatim.
  Routes: `GET /cards`, `GET /card/<id>` (id or ref), `POST /card`,
  `/comment`, `/request`, `/respond`, `/action` (move/queue/park),
  `/wiki/extension`. Supports `unreachable: true`, per-route
  `errors: {"METHOD /path": {status,error}}`, deterministic `updated`
  stamps, and a `calls` log exposed via `audit: board`.
- `dispatch_ssh:` fixture — stubs `asyncio.create_subprocess_exec`;
  per-subcommand (`status`/`start`/`followup`) stdout/rc. `audit:
  dispatch` dumps `{argv, remote}` — remote kept whole so quoted task
  text audits contiguously.
- `call:` step — invokes a runner method or dotted `module.function`
  with kwargs; exceptions wrap into the canonical `{ok:false}` shape.
  Used for `runner.devin_dispatch` (playbook path) and
  `lane_budget.check/status` (spend gates sit in front of the connect
  path, not a tool).
- `usage_log:` + `budget_state:` fixtures — seed `ADA_USAGE_LOG` and
  the provider-cap latch state file; `__PERIOD__`/`__KEYFP__`
  placeholders resolve at run time so scenarios are year-round
  deterministic.
- Clears `devin_dispatch._recent_dispatches` per scenario; restores
  patched module attributes on exit.

### Bug fix — `backend/tool_runner/__init__.py`

`confirmed`/`confirm_token` were popped in `_execute_gated` and never
reached `tools.d run()` — the hand-back inspected the
`_bound(**kwargs)` wrapper, which can never declare them. Now inspects
`dyn_spec.run` for dynamic tools. Before this, `kanban`'s internal
`_require_confirmation` gates (doing-cap, gated review->done, gated
queue) were unsatisfiable through `execute()` — the new
`kanban-move-gates` scenario pins the fix.

### Scenarios — `tests/scenarios/`

- `kanban_board_ops` — list/read/comment/file/ask + guest read/write
  boundary against the fake board.
- `kanban_move_gates` — free moves, doing-cap needs_confirm + bound
  token replay, evidence-required review->done (hard rule, no token),
  gated-card evidence move via confirmed=true, free vs gated queue,
  secondary-speaker write refusal, `audit: board` verifies the audit
  comments ("(Tony confirmed)", "verified: …").
- `devin_dispatch_board` — confirm gate, board-dispatch files an armed
  `voice-dispatch-*` card (queue:true), in-process dedup on a rephrase
  via the retired `devin_dispatch` alias, followup + answer through the
  ssh stub, audit proves `followup` ran and `start` never did.
- `devin_dispatch_board_down` — unreachable board: honest
  `{ok:false}`, no ssh fallback, infra-failure confirm_token re-arm +
  replay still fails plainly.
- `devin_dispatch_playbook` — spec_status gate refusal (no spec-review
  doc → no ssh), passing spec dispatches `start` and stamps
  `job/<task_id>/contract` into FakeMddb, `investigate` report-only
  path, owner-identity gate for a guest, `devin_read status` via stub.
- `ada_opencode_flow` — card-spec spend refusal (max_usd<=0),
  missing-limits report, preflight receipt triple captured and replayed,
  expired/forged receipt refusals, queue-through-board success,
  post-queue receipt invalidation (receipt binds `card.updated`).
- `ada_sweep` — confirm gate, dry_run reports ready/blocked/skipped
  with zero queue writes, confirmed sweep queues only the ready card.
- `wiki_extension_gate` — manifest confirmed gate on every action,
  bound-token replay, allowlist refusal never reaching the board,
  confirmed enable lands via `/wiki/extension`.
- `lane_budget_cap` — `ADA_LANE_MONTHLY_BUDGET_USD` +
  `usage_log:` → `LaneBudgetExceeded` with spend figure + hold date.
- `lane_budget_latch` — `budget_state:` latch → `ProviderCapLatched`,
  status exposes only the key fingerprint.
- `cms_write_flow` — report-meta contract gate (pre-confirm),
  register→confirm publish into FakeMddb, unregistered-slug
  confirmed=true denied, `cms_read` verify-after-write, ungated note,
  gated delete.

### `tests/benchmark.yml`

`ada_opencode_dispatch`, `ada_sweep`, `wiki_extension` added to
`write_tools`; all nine write-exercising scenario names added to
`write_allowed_in`.

## Verification

- `pytest tests/test_scenarios.py` — **30/30 pass** (19 baseline + 11 new).
- `pytest tests/test_kanban.py tests/test_devin_dispatch.py
  tests/test_wiki_extension.py tests/test_lane_budget.py
  tests/test_ada_opencode_control.py` — all pass.
- `python3 scripts/tool-lint.py` — clean.
- `tests/test_tool_runner.py` — 17 pre-existing failures
  (`ValueError != SchemaError` expectations) reproduce identically on
  the stash-cleaned baseline; unrelated to this change. Two
  `test_lane_budget` PIL import failures were my minimal venv missing
  Pillow; they pass after installing it.

## Known gaps / notes

- `wiki_read`/`wiki_search` haven't landed in this tree — the wiki
  scenario covers `wiki_extension` only; add read/search scenarios when
  those tools ship.
- The `devin` canonical tool doesn't expose `playbook`/`params` — the
  playbook scenario drives `runner.devin_dispatch` via `call:`.
- Refusal-heavy scenario steps must interleave a success between same-
  tool `ok:false` results — the storm breaker trips after 2.

lessons:
- The storm breaker counts any {ok:false} tool result toward its trip
  threshold — consecutive expected refusals on one tool name
  short-circuit later calls; interleave a success.
- confirmed/confirm_token don't survive execute() into tools.d run()
  unless run() declares them — the hand-back signature check now reads
  spec.run, not the _bound wrapper.
- FakeBoard audits: assert on card state/call log, not tool result text
  — board comments JSON-escape em-dashes (\u2014).
- The devin OpenCode receipt binds card.updated — queueing the card
  restamps it and invalidates the receipt; worth pinning in scenarios.
