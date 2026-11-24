"""Test-package env pins — the unittest-side twin of tests/conftest.py.

pytest loads conftest.py before any test module, but unittest-style runs
(`python -m unittest tests.test_x`) import this package __init__ instead
and never see conftest. The pins keep those runs deterministic (same
values conftest pins: instance identity, API-key stub, ledger off) —
test_lane_budget expects lane "test", and an unset ADA_INSTANCE_ID would
also leave lane="" where pytest sees "test".

Usage-ledger pollution is primarily gated in backend/usage_tracker.py
(implicit data/usage.jsonl persists only for lane-identified processes —
the t-ctx incident fix, card live-voice-burn-context-replay); the explicit
ADA_USAGE_LOG=off here is belt on top of that gate for package-import
runs. conftest.py sets the same values; keep them in sync.

Residual: `python -m unittest discover -s tests` (top_level=tests) imports
test modules as top-level test_* and bypasses this file — covered by the
ledger lane gate instead; no known runner uses that form.
"""
import os

os.environ["ADA_INSTANCE_ID"] = "test"
os.environ.setdefault("GEMINI_API_KEY", "x")
os.environ["ADA_USAGE_LOG"] = "off"
