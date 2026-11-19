#!/usr/bin/env bash
# sync-ocb-bridge.sh — keep the stable ada-control bridge checkout on origin/main.
#
# The bridge checkout (default ~/CascadeProjects/ada-bridge) is a dedicated
# `git worktree` of the ada-voice checkout (default ~/CascadeProjects/ada-voice).
# It exists ONLY to serve opencode/ada_mcp.py to OpenChamber/OpenCode MCP
# clients: their opencode.json `mcp.ada-control.command` points at
# $ADA_BRIDGE_CHECKOUT/opencode/ada_mcp.py, so an MCP child always runs the
# main-branch copy instead of whichever edit/staging checkout it was wired to.
#
# Invariants enforced here (the checkout "only ever tracks origin/main"):
#   - detached HEAD — never a local branch
#   - clean tree — a dirty tree REFUSES the sync (report, exit 2); nothing is
#     touched. Hand-edits in the bridge checkout are never valid.
#   - fast-forward only — local commits in the checkout (HEAD ahead of
#     origin/main) refuse the same way.
#   - opencode/ada_mcp.py must exist and be tracked — a checkout that can't
#     serve the bridge refuses.
# A refused sync exits 2 so the timer unit shows a failed run in
# `journalctl --user -u ada-ocb-bridge-sync` instead of silently drifting.
#
# Bonus: because the path is a real worktree, ada_mcp.py's bridge-version
# resolution (`git describe --always --dirty --tags` in the file's dir) names
# the exact origin/main commit serving each MCP session — `ada_bridge_info`
# and calls.jsonl prove which revision a client ran.
#
# Usage:  sync-ocb-bridge.sh [--dry-run]
#   --dry-run  fetches and reports what WOULD change (worktree create /
#              N-commit fast-forward / refusal) but never touches the checkout.
# Env:
#   ADA_BRIDGE_CHECKOUT  bridge checkout path   (default ~/CascadeProjects/ada-bridge)
#   ADA_VOICE_REPO       ada-voice checkout that owns the worktree
#                        (default ~/CascadeProjects/ada-voice)
#   ADA_REMOTE           remote name            (default origin)
#   ADA_BRANCH           tracked branch         (default main)
# Exit: 0 ok (created / fast-forwarded / already current)
#       1 operational error (repo missing, fetch failed, ...)
#       2 refused — dirty tree, local commits, missing ada_mcp.py

set -euo pipefail

DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
        *) echo "[ocb-bridge] unknown arg: $arg" >&2; exit 1 ;;
    esac
done

REPO="${ADA_VOICE_REPO:-$HOME/CascadeProjects/ada-voice}"
CHECKOUT="${ADA_BRIDGE_CHECKOUT:-$HOME/CascadeProjects/ada-bridge}"
REMOTE="${ADA_REMOTE:-origin}"
BRANCH="${ADA_BRANCH:-main}"
REF="$REMOTE/$BRANCH"
BRIDGE_FILE="opencode/ada_mcp.py"

log()  { printf '[ocb-bridge] %s\n' "$*"; }
die()  { printf '[ocb-bridge] FAIL: %s\n' "$*" >&2; exit 1; }
refuse() { printf '[ocb-bridge] REFUSE: %s\n' "$*" >&2; exit 2; }

# --- 0. sanity -------------------------------------------------------------
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 \
    || die "ada-voice repo $REPO not found or not a git checkout"

# --- 1. fetch --------------------------------------------------------------
# Always fetch (even --dry-run): it updates the shared remote-tracking ref,
# not the checkout — without it a dry run would report against stale state.
git -C "$REPO" fetch "$REMOTE" --quiet || die "git fetch $REMOTE failed"
git -C "$REPO" rev-parse --verify --quiet "$REF" >/dev/null \
    || die "$REF not found after fetch"
want=$(git -C "$REPO" rev-parse "$REF")

# --- 2. create the worktree if absent ---------------------------------------
if [[ ! -e "$CHECKOUT/.git" ]]; then
    if [[ -d "$CHECKOUT" && -n "$(ls -A "$CHECKOUT" 2>/dev/null)" ]]; then
        refuse "$CHECKOUT exists, is not a git checkout, and is not empty — refusing to clobber it"
    fi
    if [[ "$DRY_RUN" == "1" ]]; then
        log "dry-run: would create worktree $CHECKOUT at $REF (${want:0:12})"
        exit 0
    fi
    log "creating worktree $CHECKOUT at $REF (${want:0:12})"
    git -C "$REPO" worktree add --detach "$CHECKOUT" "$REF" >/dev/null \
        || die "git worktree add failed"
fi

git -C "$CHECKOUT" rev-parse --git-dir >/dev/null 2>&1 \
    || die "$CHECKOUT is not a git checkout"

# --- 3. refuse a dirty or diverged checkout ---------------------------------
dirty=$(git -C "$CHECKOUT" status --porcelain)
if [[ -n "$dirty" ]]; then
    printf '[ocb-bridge] dirty paths in %s:\n%s\n' "$CHECKOUT" "$dirty" >&2
    note=""
    git -C "$CHECKOUT" status --porcelain -- "$BRIDGE_FILE" | grep -q . \
        && note=" ($BRIDGE_FILE itself is dirty)"
    refuse "bridge checkout is dirty$note — the bridge must only ever track $REF; inspect/repair by hand (e.g. git -C $CHECKOUT status), do not commit there"
fi

head=$(git -C "$CHECKOUT" rev-parse HEAD)
ahead=$(git -C "$CHECKOUT" rev-list --count "$REF..HEAD")
[[ "$ahead" == "0" ]] \
    || refuse "bridge checkout has $ahead local commit(s) on top of $REF — refusing to move it; the bridge must only ever track $REF (inspect: git -C $CHECKOUT log $REF..HEAD)"

git -C "$CHECKOUT" ls-files --error-unmatch "$BRIDGE_FILE" >/dev/null 2>&1 \
    || refuse "$BRIDGE_FILE not present/tracked in $CHECKOUT — checkout cannot serve the bridge"

# --- 4. fast-forward ---------------------------------------------------------
behind=$(git -C "$CHECKOUT" rev-list --count "HEAD..$REF")
if [[ "$behind" == "0" ]]; then
    log "already at $REF (${head:0:12})"
    exit 0
fi

if [[ "$DRY_RUN" == "1" ]]; then
    log "dry-run: would fast-forward $CHECKOUT by $behind commit(s) ${head:0:12} -> ${want:0:12}"
    exit 0
fi

git -C "$CHECKOUT" merge --ff-only --quiet "$REF" \
    || die "ff-only merge to $REF failed (checkout diverged?)"
log "fast-forwarded ${head:0:12} -> ${want:0:12} ($behind commit(s))"

# --- 5. post-verify ----------------------------------------------------------
# The file must still be there and clean after the move, and we report the
# describe string ada_mcp.py itself will announce as bridge version.
[[ -f "$CHECKOUT/$BRIDGE_FILE" ]] \
    || die "$BRIDGE_FILE missing after fast-forward — bridge would be broken"
git -C "$CHECKOUT" status --porcelain -- "$BRIDGE_FILE" | grep -q . \
    && die "$BRIDGE_FILE dirty after fast-forward — bridge is tainted, inspect by hand"

desc=$(git -C "$CHECKOUT" describe --always --dirty --tags 2>/dev/null || echo "$want")
log "synced: $CHECKOUT now serves $BRIDGE_FILE @ git-$desc"
