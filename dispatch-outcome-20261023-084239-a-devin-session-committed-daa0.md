# dispatch outcome — relays-remote-whisper-review

**Task:** review rescued commit daa0e0a (idc03 deploy-only incident,
2026-10-14 ~22:28; rescued as origin branch `relays-remote-whisper` =
6ad4ac7), merge to main if wanted, redeploy; remind the authoring session
that idc03 is deploy-only.

## Review verdict

- **tg_relay.py half: dropped — superseded.** Since the commit's base
  (3aae927), main shipped a strictly better remote-STT path:
  `TG_WHISPER_URL` remote-first (c8c07c7), silence-detect + noise filter
  (a3f905e), Gemini multimodal fallback + `(text, kind)` contract
  (b205457). The rescued tg code conflicts and would regress all of it.
- **line_relay.py half: wanted, but not as written.** LINE had no remote
  STT. The rescued `_decode_audio` depends on ffmpeg/imageio-ffmpeg —
  idc03 has no ffmpeg (that's why main's tg path decodes via
  av+soundfile), and its remote POST sends the raw clip instead of the
  established decoded-WAV + `model` field contract.
- **requirements.txt:** carried over as bounded ranges
  (`faster-whisper`, `soundfile`); `imageio-ffmpeg` deliberately dropped.

## What shipped (af310ef, merged to main via rebase + ff push)

`backend/line_relay.py` gains, in main's tg_relay idiom:
- `_decode_wav16k` — clip → 16k mono WAV via av+soundfile, returns peak
- `_transcribe_remote` — OpenAI-style `/v1/audio/transcriptions` POST
  (wav + `model` [LINE_WHISPER_REMOTE_MODEL, default large-v3] +
  optional `prompt` [LINE_WHISPER_PROMPT]); dead-mic clips
  (peak<0.01) skip the HTTP call
- `_transcribe` — `LINE_WHISPER_URL` remote-first, local faster-whisper
  unchanged as fallback; `str` return contract preserved (existing
  patched tests unaffected)
- env docs + `.env.example`; `LINE_WHISPER_MODEL` keeps its local-model
  meaning (default base)

## Gates + deploy

- py_compile + tool-lint clean locally; pytest in idc03's venv against a
  detached worktree of the branch: **1637 passed, 1 skipped**
- Rebased onto moved main (cbe969c), pushed `HEAD:main` (ff) → af310ef
- `deploy-ada.sh` on idc03: snapshotted a pre-existing dirty tree to
  `wip/deploy-snapshot-20261023-085154`, ff-merged e0b488a→af310ef,
  tool-lint + pytest gates passed (1655), 45s drain expired with live
  sessions (ada-ha-tony:10, ada-kk:1 — transcripts append per turn),
  all 10 services restarted and verified **active**
- ada-line-relay up, quota probe healthy; `LINE_WHISPER_URL` is NOT yet
  in the unit env — feature is dormant until Tony sets it. Note: LINE
  monthly push quota read 300/300 used at deploy time (pre-existing).

## Trail

- SSOT: `docs/ssot/jobs/ada/2026-10-23-line-remote-whisper.yml`
- Board comments posted on `relays-remote-whisper-review`, including the
  deploy-only reminder. The authoring session is not identifiable from
  the trail (commit author is the shared Devin bot identity); the rule
  is already in AGENTS.md.

## Verify

- `git log origin/main -1` → af310ef; `ssh idc03 'cd
  ~/CascadeProjects/ada-voice && git log -1 --oneline'` → af310ef
- `systemctl --user is-active ada-line-relay` on idc03 → active
- To enable: set `LINE_WHISPER_URL=http://<omen>:<port>/v1/audio/
  transcriptions` (+ `LINE_VOICE_TRANSCRIBE=1`) in the ada-line-relay
  unit env and restart.
