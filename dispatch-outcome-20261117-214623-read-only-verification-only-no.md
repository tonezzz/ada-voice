# dispatch-outcome: read-only verification of ada_mcp board read path

Card: `ada-ocb-kanban-smoke` (ADA-0173) — runner tony-omen — 2026-11-17
Scope: read-only only. No dispatch/queue/hold/move/merge/deploy/credentials/destructive ops performed.

## What was done

Imported `opencode/ada_mcp.py` (HEAD `ff2d6a1`, the fast-board-reads commit) with
system `python3` (`mcp` 1.28.1 installed) and exercised the board read path against
the live board-api `https://idc03.taila0626a.ts.net:10000`. Snapshot cache was
cleared before each timed call so elapsed reflects the real fetch path. Runner
script kept at `verify_board_reads.py` in the worktree root for re-verification.

## Results — all steps ok, zero mutations

| step | elapsed | result |
|---|---|---|
| `find_card('ada-ocb-kanban-smoke')` | 0.997s | ok → ADA-0173 "ada-ocb kanban smoke…" (doing) |
| `find_card('ADA-0172')` | 0.837s | ok → `ada-ocb-board-read-latency` (review) |
| `_board_cards()` | 2.075s | ok, 914 rows (projected fields) |
| `_compact_cards()` | 0.609s | ok, 914 rows |
| `_pick_cards(sweep, 7, 3)` | 2.373s | ok, 3 picks |
| `ada_card_pick(sweep, 3)` | 2.342s | ok, 3 picks, `selection_token` on every pick, `mutations_performed=[]` |
| `_pick_cards(queue, 7, 3)` | 2.563s | ok, 1 pick |
| `ada_card_pick(queue, 3)` | 1.932s | ok, 1 pick, `selection_token` present, `mutations_performed=[]` |
| `_context_card('ada-ocb-kanban-smoke', +15s)` | 0.301s | ok, full fields incl `comms` (6 entries) |

Notes:
- All `find_card` resolutions were far under the 10s post-`ff2d6a1` expectation
  (compact-index exact hit + single-card hydrate).
- Row counts match the ~910 expectation exactly (914).
- `_context_card` returned every requested field except `requests`; a direct
  `/cards?id=…&fields=…requests…` query showed the board-api omits `requests`
  entirely when it is empty — expected server behavior, not a read-path defect.
- Recursive scan found no non-empty `mutations`/`mutations_performed` in any
  return value; `ada_card_pick` only populates the in-process
  `PENDING_NEXT_SELECTIONS` token map. Read-only confirmed.

## Evidence

One evidence comment posted to `ada-ocb-kanban-smoke` via board-api `/comment`
listing each step, elapsed time, and ok status (plus one earlier "running"
progress comment). `{"ok": true, "message": "comment added"}`.

## How to verify

```
python3 verify_board_reads.py   # from the worktree root
curl -s "https://idc03.taila0626a.ts.net:10000/cards?id=ada-ocb-kanban-smoke&fields=id,ref,comms" | jq '.cards[0].comms[-2:]'
```
