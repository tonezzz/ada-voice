# Dispatch outcome — ada-printer-diagnostics-safe-repair (ADA-1049)

Attempt 3, runner idc02 (attempts 1–2 on tony-omen died on provider
exits before producing code). Result: **implemented, tested, live-
verified on the print host — not deployed, no queue retargeted.**

## What changed

- `scripts/ada-cups-helper.py` — narrow print-host helper; ops =
  `status | jobs | verify | release-job | retry-job | cancel-job |
  set-queue-uri`. Resolves `dnssd://`/`ipps://` queue URIs
  (avahi-browse/avahi-resolve), probes IPP per queue, labels jobs
  `origin: cups` vs `printer-internal`, and refuses `set-queue-uri`
  unless `--expect-serial`/`--expect-uuid` match the live probe.
  Installed on tony-dell at `~/bin/ada-cups-helper.py`.
- `backend/printer_registry.yml` — authoritative identity map
  (serial `CN33BDZ0KS`, uuid `…b906fe`, mDNS `HPE073E705C516.local`,
  last_verified_ip `.92`, three named stale targets, three queues).
- `backend/printer_client.py` — async ssh lane (`ssh tony-dell <helper>
  <op>`, JSON only, env-overridable) + `evaluate_status()` identity/
  drift/remediation verdicts.
- `backend/tool_runner/printer.py` — canonical `printer` tool:
  `status|jobs|verify` read-only; `repair` = fixed allowlist
  (release|retry|cancel one verified job) behind the existing
  confirmed=true/bound-`confirm_token` gate, re-reads the queue after
  acting, returns an audit block, logs `printer_repair` session event
  + change-request.
- Wiring: `common.py` (PRINTER_TOOLS gate set, SECONDARY_BLOCKED —
  owner-tier, CONFIRM_GATED), `__init__.py` (mixin + elif + repair-only
  change-log), `realtime_provider.py` (declaration, `_CLAIM_BACKING_
  ACTIONS`, actuation budget), `tool_guide.yml`,
  `ssot.tool-surface.yml` (cap 114→115), scenario
  `tests/scenarios-live/printer_diag.yaml`, `tests/test_printer.py`
  (25 tests), SSOT doc
  `docs/ssot/jobs/ada/2026-11-23-printer-diagnostics-safe-repair.yml`.

## Found (identity drift — deeper than the card knew)

- Printer verified live at **192.168.2.92** (card said .78 — already
  superseded by DHCP): serial `CN33BDZ0KS`, uuid
  `5ba42c21-ae68-7f6d-830e-600ee5b906fe` (card's `…b90fe6` is a digit
  transposition — registry holds the probed value), idle, accepting,
  ink 70/40.
- `HP_DeskJet_2700` (default queue) targets `ipp://192.168.2.75` —
  the wireless repeater, Host is down. **Not retargeted**: Tony's
  confirmed scope covers single-job repair only; queue reconfiguration
  needs separate approval — board request raised, no reply by end.
- `HP-2777` (dnssd) and `HP_DeskJet_2700_series_05C516` (CUPS-auto
  driverless, ipps://mDNS) both follow the printer and verify `match`.

## Live verification on tony-dell (staging)

- `status`: both verdicts correct — `HP-2777` match/follows_mdns,
  `HP_DeskJet_2700` unreachable + stale_target + remediation hint.
- Repair exercised for real: submitted held job `HP_DeskJet_2700-19`
  to the dead-target queue → `release-job` (held→processing),
  `retry-job` honest-fail ("job not complete"), `cancel-job` removed
  it. Negatives refused: job 9999 unlisted, `cancel-job -a` bad id,
  unknown op rejected. Queue left empty — no paper, no residue.

## Gates

- `pytest tests/test_printer.py` — 25 passed, 0 warnings.
- `python3 scripts/tool-lint.py` — clean (71 declared, cap 115).
- `pytest tests/ -x -q` — **1879 passed, 7 skipped** (4m15s).
- No commit, no push, no deploy. `git status` shows only the files
  listed above.

## How to verify

1. `cd <worktree>`; `pytest tests/test_printer.py -x -q`.
2. `python3 scripts/tool-lint.py`.
3. Live: `ssh tony-dell 'python3 ~/bin/ada-cups-helper.py status'`.
4. Retarget (after Tony approves): on tony-dell
   `python3 ~/bin/ada-cups-helper.py set-queue-uri HP_DeskJet_2700
   ipp://192.168.2.92/ipp/print --expect-serial CN33BDZ0KS
   --expect-uuid 5ba42c21-ae68-7f6d-830e-600ee5b906fe` then re-run
   `status` and update registry `device_uri_observed`/`status`.

lessons:
- idc02 cannot route 192.168.2.x — CUPS/IPP probes on this LAN go via
  `ssh tony-dell`, and non-interactive ssh PATH skips ~/.local/bin, so
  the helper must be invoked by full path (`python3 ~/bin/…`).
- dnssd:// CUPS URIs embed the *service instance name*, not a
  resolvable host — resolve with `avahi-browse -rtp _ipp._tcp` and
  match the unescaped instance, don't avahi-resolve the URI host.
- Card-provided IPs/UUIDs can themselves be stale (`.78`→`.92`,
  `…b90fe6`→`…b906fe`) — registry must anchor on probed serial/uuid.
- `ipptool` reports bare job ids (`19`) while lpstat/CUPS spell
  `queue-19` — normalize both directions when verifying repair targets.
- CUPS auto-creates driverless queues (e.g. `ipps://…05C516`) on
  activity — registry/evaluate must tolerate queues beyond the seeded
  list.
