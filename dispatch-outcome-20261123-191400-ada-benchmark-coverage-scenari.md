# Dispatch outcome — ada-benchmark-coverage-scenarios (ADA-1204)

Card: ada-benchmark-coverage-scenarios-for-kan-152a4f · attempt 3
Branch: dispatch/20261123-191400-ada-benchmark-coverage-scenari

## What happened

The 2026-11-19 run produced the full deliverable — 11 new offline
scenarios + FakeBoard/dispatch_ssh/call/usage_log/budget_state engine
fixtures + a real tool_runner confirm hand-back fix — on
`origin/dispatch/20261119-171510-ada-benchmark-coverage-scenari`
(a77e51e + b25eaa0). It never merged: the merge gate failed on
`test_scenarios.py::ScenarioTests::test_cms_write_flow` and the branch
sat unmerged until today's verify-fail requeue.

This run cherry-picked both commits cleanly onto current main and fixed
the actual gate failure. Root cause was NOT flakiness — it was an
env-dependent test: the scenario was authored and "verified" in a
dep-stubbed interpreter where `declared_tool_schemas()` raises, so
`_BUILTIN_SCHEMAS={}` and `_validate_call_args()` never ran. Under the
real gate venv the lazy-schema arg validation fires and rejected two
steps:

1. `cms_publish_page` called with absent summary/domain/fresh_for/
   confidence → SchemaError "call args rejected: missing required ..."
   before the tool's "report meta contract" could fire. Fixed by
   passing the fields present-but-empty — schema shape checks pass and
   `missing_publish_fields` still rejects deterministically in both
   envs.
2. `cms_read action=get` used the `slug=` alias, not a declared property
   → SchemaError "unknown args 'slug'". Fixed to canonical `key=`.

## Result

- `pytest tests/ -q` (CI deselects, real deps via uv venv py3.12):
  **1902 passed, 1 skipped, 5 deselected** — was 1 failed before the fix.
- `pytest tests/test_scenarios.py`: 30/30 offline scenarios green.
- `scripts/scenario-schema-check.py`: 208 files, 0 violations.
- `scripts/tool-lint.py`: clean (69 warnings, advisory level).
- Commits: 1f6fca6 (cherry-pick of a77e51e), 21840d9 (cherry-pick of
  b25eaa0 outcome doc), d83d76c (cms_write_flow schema-safe args), plus
  this outcome + docs/ssot/jobs/ada/2026-11-23-scenario-schema-clean-args.yml.

## How to verify

```
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r <(grep -vE '^(torch|speechbrain)([=<>]|$)' backend/requirements.txt) pytest pytest-asyncio pyyaml
.venv/bin/python -m pytest tests/test_scenarios.py -q   # 30 passed
.venv/bin/python -m pytest "tests/test_scenarios.py::ScenarioTests::test_cms_write_flow" -q
```

Note: running the scenario suite in a bare interpreter (no
google-genai/fastapi) masks schema-validation failures — always verify
against the CI-parity dep set.

lessons:
- Scenario `tool:` steps must use declared-schema-clean args (declared names, required keys present, no unknowns) — `_validate_call_args` is inert in dep-stubbed envs, so "green" there does not prove green at the gate.
- Present-but-empty strings pass schema `required`/type checks but still trip tool-level blank-field contracts — the deterministic probe for second-layer validation.
- Tool-signature aliases (e.g. cms_read slug=) are not schema-declared; use the canonical arg name in scenarios.
- A merge-gate failure signature pointing at an unrelated log line (mddb_client.py:244 ERROR) was a red herring — the real failure was the scenario assertion two lines up; read the FAILURES block, not the log.
