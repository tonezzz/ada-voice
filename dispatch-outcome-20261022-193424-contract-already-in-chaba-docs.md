# Dispatch outcome — ada-memory-dev-lane-runtime (contract impl)

Card: `ada-memory-dev-lane-runtime`. Worktree only; no commit, no push,
no deploy (card requires explicit approval for idc03 render/restart).

## What changed

The mount mechanics already existed (b453314). This session completed
the contract:

- `backend/memory_banks.py`
  - `MemoryBank` gains `readonly: bool = False`,
    `env: str | None = None`, `read_as: str | None = None`
    (read_as recorded for /api/memory/banks clarity).
  - `_add()` computes
    `read_as = ro_map.get(self.instance) if isinstance(ro_map, dict) else None`
    before the instances check; keeps the bank when
    `self.instance in instances or read_as`; expands `{instance}` with
    `read_as or self.instance`; when `read_as` forces `writable=False`
    and `allowed_tools=[]`. The multi-instance `scope=instance` guard
    still evaluates `spec["instances"]`.
  - `_acl_bank()` writable layer: a readonly-mounted bank refuses with
    "read-only-mounted on instance '<id>' (mirroring '<prod>')" and
    names `dev-scratch` as the write lane when a writable dev-scratch is
    assigned to the instance. Plain read-only banks keep the classic
    "is read-only" message.
- `backend/memory_ops.py` — `memory_meta()`: `meta["env"] = [bank.env]`
  when the bank declares env (dev-scratch: staging); prod banks emit no
  env field.
- `backend/tool_runner/__init__.py` — `_check_memory_write_allowed`
  surfaces `decision["reason"]` for the `bank.writable` layer so the
  richer mount message reaches the model (identical text otherwise).
- `tests/test_memory_banks.py` — `_dev_lane_spec()` helper + 6 tests:
  tony-dev↔tony bank-set parity (all readonly, personal →
  `ada-ha-bank-personal-tony`), `dev` instance isolated to dev-scratch,
  person.kk allow-list on tony-dev (personal-kk not mounted;
  dev-scratch sole writable), unknown spec fields + non-dict
  `instances_readonly` tolerated, env meta marker, refusal naming
  dev-scratch (registry and runner level).
- `docs/ssot/jobs/ada/2026-10-22-memory-dev-lane-runtime.yml` — trail.

## Result

- `pytest tests/ -x -q` → **1621 passed, 7 skipped** (in
  `.venv-dispatch`, a uv venv inside the worktree — self-ignored via its
  own .gitignore; deps installed: pytest, httpx, pyyaml, websockets,
  pillow, numpy, uvicorn[standard], fastapi==0.116.1).
- `python3 scripts/tool-lint.py` → clean (61 pre-existing warnings).
- 93/93 in test_memory_banks.py.

## How to verify

```
.venv-dispatch/bin/python -m pytest tests/test_memory_banks.py -q
python3 - <<'EOF'
import json, tempfile
from backend.memory_banks import MemoryBankRegistry
spec = {"banks": {"personal": {"scope": "instance", "instances": ["tony"],
  "instances_readonly": {"tony-dev": "tony"},
  "mddb_collection": "ada-ha-bank-personal-{instance}", "writable": True}}}
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
    json.dump(spec, f)
r = MemoryBankRegistry(path=f.name, instance="tony-dev")
b = r.bank("personal")
print(b.mddb_collection, b.writable, b.readonly, b.read_as)
print(r.acl_decision("person.tony", bank="personal", tool="ada_remember")["reason"])
EOF
```
Expected: `ada-ha-bank-personal-tony False True tony` and a refusal
saying `read-only-mounted`.

## Not done (per card)

Deploy is manual: `render-memory-banks.py --host idc03` then restart
`ada-dev` + `ada-ha-*` — needs explicit approval.
