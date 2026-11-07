# Dispatch outcome — ada-render-video-line-unsendable

**Card:** `ada-render-video-line-unsendable` (ref ADA-0512)
**Task:** `20261026-070636-hybrid-delivery-per-tony-line-` on idc02
**Result:** implemented + tested. LINE lanes now get a poster jpeg +
playable link; TG lanes keep file delivery via sendVideo. Uncommitted
in worktree (dispatch convention); not pushed, not deployed.

## What changed

**`backend/line_relay.py`** — the video/link output path:

- `_video_link_msgs(url, caption)` replaces `_video_link_msg`: resolves
  the URL (doc-media + relative), fetches the clip relay-side (the
  relay host rides the tailnet, so `*.ts.net` render URLs resolve),
  extracts the first frame via the existing `_video_poster` (PyAV) and
  hosts it publicly on the `/relay-img/<vpost-nonce>` IMG_CACHE_DIR
  lane → `[imageMessage(poster), text(caption + link)]`. Any
  fetch/poster failure degrades to the same bare caption+link text
  ADA-0512 sent. Both the turn path (`_media_msgs`) and the loopback
  `/send` contract use it.
- `_tailnet_only_url(url)` — `*.ts.net`, localhost, and any
  non-`is_global` IP (loopback, RFC1918, link-local, and the 100.64/10
  CGNAT range `is_private` lets through) count as tailnet-only.
- `_arch_host_video(data, mime)` — when the source URL is tailnet-only,
  the fetched bytes are persisted under
  `MEDIA_ARCHIVE_DIR/video/clip-<ts>-<rand>.<ext>` and the text link
  becomes the durable `{LINE_PUBLIC_IMG_BASE}/arch/video/<name>` URL —
  playable for members off-tailnet. Non-video bytes (HTML error pages)
  are never re-hosted.
- `_mine_image_urls` skips video tails (.mp4/.m4v/.mov/.webm/.mkv) so a
  narrated clip link isn't image-mined into a doomed multi-MB fetch.
- `_video_msgs` (native videoMessage path) stays for a policy flip-back.

**`backend/tg_relay.py`** — no change needed: turn media and `/send`
`video_url` already route to `send_video` → `sendVideo` multipart
upload (commit b36c734). Locked the contract with tests instead.

**`backend/tools.d/ada_render_video.py` + `backend/tool_guide.yml`** —
result `note` and guide now say relays auto-deliver (LINE: poster +
link, TG: the file) so the model doesn't fire a redundant chat_send.

**`tests/test_line_video_link.py`** — rewritten for the hybrid policy:
tailnet-host detection, fetch-failure → bare link, public URL →
poster+original link (no re-host), tailnet URL → re-hosted arch link
with on-disk bytes verified, poster failure → link only.

**`tests/test_tg_relay_video.py`** — new: send_media video → sendVideo
multipart carrying the real clip bytes; image kind still sendPhoto;
fetch failure and >49MB clips → text-link fallback; `video_url` →
kind "video" detection.

**`docs/ssot/jobs/ada/2026-10-26-hybrid-video-delivery.yml`** — trail.

## Judgment calls

- "tailnet link" in the spec was read as "the render's link" — when the
  source URL is tailnet-only the link sent is the *public* arch re-host
  (strictly better: the card's own failure was a ts.net URL members
  off-tailnet couldn't open). Public sources keep their original link.
- Re-host goes to the durable arch lane (spec's "doc-store asset"
  alternative), not the TTL'd nonce lane — a 1h-expiring link would not
  stay "playable". The poster itself lives on the nonce lane like every
  other outbound image.

## Verification

- `pytest tests/test_line_video_link.py tests/test_tg_relay_video.py
  tests/test_line_relay_*.py tests/test_tg_relay_*.py
  tests/test_render_video.py -x -q` → 134 passed.
- Full suite: `pytest tests/ -x -q` → **1701 passed, 4 skipped**
  (fresh `.venv-test`, removed after).
- `python3 scripts/tool-lint.py` → clean (57 declared, cap 112).
- Live local smoke: real PyAV-encoded mp4 served on 127.0.0.1 →
  `_video_link_msgs` returned `[image poster (real \xff\xd8\xff jpeg
  persisted to img-cache), text "demo clip\n<pub>/arch/video/clip-*.mp4"]`.
- Not exercised: real LINE/TG pushes and a billed Veo render — both
  need live credentials on idc03. Post-deploy: render on a LINE lane →
  poster bubble + `…/relay-img/arch/(renders|video)/….mp4` link; on a
  TG lane → native playable sendVideo.
