#!/usr/bin/env bash
# Update an installed agent-relay checkout and restart its service when the
# code changed.
#
# Safe to run repeatedly: when HEAD already matches the default branch nothing
# is restarted, unless a previous run deferred the restart — that pending
# restart is then completed. Because tasks and sessions live in memory, the
# first restart is skipped while the relay reports queued or running tasks
# (or while its health cannot be read at all); the deferral is recorded in a
# marker and the next run escalates: after one full deferral the restart
# happens even when the verdict is still "unknown", so an old relay that
# cannot report counts is updated at most one cycle late instead of never.
# Only a relay that visibly reports busy tasks is deferred again. --force
# restarts immediately; --no-restart records the deferral without attempting
# a restart, so a later plain run completes it.
#
# Usage:
#   scripts/update.sh [--force] [--no-restart]
#
# Env overrides (same as setup.sh):
#   AGENT_RELAY_DIR   install directory (default: ~/.local/share/agent-relay)
#   AGENT_RELAY_REPO  git URL, inherited by the bundled setup.sh when this
#                     script bootstraps a missing checkout
#   A2A_RELAY_PORT    relay port for the health check (default: 43124)
set -euo pipefail

TARGET_DIR="${AGENT_RELAY_DIR:-$HOME/.local/share/agent-relay}"
PORT="${A2A_RELAY_PORT:-${AGENT_RELAY_PORT:-43124}}"
LABEL="com.agent-relay"
PENDING="$TARGET_DIR/.git/agent-relay-pending-restart"
# Written next to the pending marker, inside .git, so the checkout stays clean
# and /healthz can report the last attempt without a log dive.
STATUS="$TARGET_DIR/.git/agent-relay-update-status.json"
FORCE=0
NO_RESTART=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --no-restart) NO_RESTART=1 ;;
    *) echo "usage: $0 [--force] [--no-restart]" >&2; exit 2 ;;
  esac
done

health() { curl -sS --max-time 2 "http://127.0.0.1:$PORT/healthz" 2>/dev/null || true; }

# busy() echoes one word:
#   idle    - health answered ok and active + queued == 0
#   busy    - health answered ok and tasks are in flight
#   unknown - no answer, bad answer, or a relay that does not report counts
busy() {
  local h
  h="$(health)"
  if [ -z "$h" ] || ! printf '%s' "$h" | grep -q '"ok": *true'; then
    echo unknown; return
  fi
  printf '%s' "$h" | python3 -c '
import json, sys
d = json.load(sys.stdin)
if "active" not in d or "queued" not in d:
    print("unknown")
else:
    print("busy" if d["active"] + d["queued"] > 0 else "idle")
' 2>/dev/null || echo unknown
}

# Record what this run did where the relay can read it: GET /healthz reports
# this file, so a deferred restart or a failed pull is visible without a dive
# into the log. Renamed into place, so a reader never sees a half-written
# record; failing to record warns and never changes the outcome of the update.
record_status() {
  local outcome="$1" pending="$2"
  local tmp="$STATUS.tmp.$$"
  if printf '{"at":"%s","outcome":"%s","from":"%s","to":"%s","pendingRestart":%s}\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$outcome" "${OLD_HEAD:-}" "${NEW_HEAD:-}" "$pending" \
      > "$tmp" && mv -f "$tmp" "$STATUS"; then
    return 0
  fi
  rm -f "$tmp"
  echo "warning: could not record the update status at $STATUS" >&2
}

# true when a restart is still recorded as pending, else false.
pending_flag() {
  if [ -f "$PENDING" ]; then printf true; else printf false; fi
}

restart_service() {
  if [ "$(uname -s)" = "Darwin" ]; then
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    if [ -f "$PLIST" ]; then
      launchctl kickstart -k "gui/$(id -u)/$LABEL"
    else
      echo "warning: no $PLIST; start the relay manually or run scripts/install-service.sh" >&2
      return 1
    fi
  else
    if systemctl --user cat agent-relay.service >/dev/null 2>&1; then
      systemctl --user restart agent-relay.service
    else
      echo "warning: no agent-relay systemd user unit; start the relay manually or run scripts/install-service.sh" >&2
      return 1
    fi
  fi
}

# Wait for the restarted relay to answer /healthz, bounded by wall-clock time
# (~15s) rather than by probe attempts.
await_health() {
  local deadline=$((SECONDS + 15))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if health | grep -q '"ok": *true'; then return 0; fi
    sleep 0.5
  done
  return 1
}

# 1. Locate the checkout; bootstrap it via the bundled setup script when missing.
if [ ! -d "$TARGET_DIR/.git" ]; then
  echo "no checkout at $TARGET_DIR; running setup"
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  SETUP="$SCRIPT_DIR/../skills/agent-relay/scripts/setup.sh"
  if [ ! -f "$SETUP" ]; then
    echo "error: setup script not found at $SETUP" >&2
    exit 1
  fi
  exec bash "$SETUP"
fi

# 2. Fast-forward to main. On the no-op path, finish a restart deferred by an
#    earlier run instead of exiting: the code is already new, only the running
#    process may still be old.
OLD_HEAD="$(git -C "$TARGET_DIR" rev-parse HEAD)"
if ! git -C "$TARGET_DIR" pull --ff-only origin main; then
  echo "warning: could not fast-forward to main; the checkout may need manual attention" >&2
  record_status failed "$(pending_flag)"
  exit 1
fi
NEW_HEAD="$(git -C "$TARGET_DIR" rev-parse HEAD)"

PENDING_RESTART=0
if [ "$OLD_HEAD" = "$NEW_HEAD" ]; then
  if [ -f "$PENDING" ] && [ "$(cat "$PENDING" 2>/dev/null)" = "$NEW_HEAD" ]; then
    PENDING_RESTART=1
    echo "already up to date; completing a restart deferred earlier"
  else
    echo "already up to date ($NEW_HEAD)"
    record_status up_to_date false
    exit 0
  fi
else
  echo "updated: $OLD_HEAD -> $NEW_HEAD"
fi

# 3. --no-restart always defers: record the marker and leave the service alone.
if [ "$NO_RESTART" -eq 1 ]; then
  STATE="busy: $(busy)"
  printf '%s' "$NEW_HEAD" > "$PENDING"
  echo "--no-restart: restart deferred ($STATE); the next run without --no-restart will complete it"
  record_status updated true
  exit 0
fi

# 4. Decide whether to restart now. First sight of an update waits for an idle
#    relay (fail-closed). Once this exact update has been deferred (marker
#    matches HEAD), the next run escalates: it restarts unless the relay
#    visibly reports busy tasks, so an old relay that cannot report counts is
#    activated one cycle late instead of never. --force always restarts.
STATE="busy: $(busy)"
if [ "$STATE" = "busy: idle" ] || [ "$FORCE" -eq 1 ] \
    || { [ "$PENDING_RESTART" -eq 1 ] && [ "$STATE" != "busy: busy" ]; }; then
  # Mark first, clear on success: a failed restart command or a failed health
  # check keeps the marker so the next run retries the restart.
  printf '%s' "$NEW_HEAD" > "$PENDING"
  if ! restart_service; then
    record_status failed true
    exit 1
  fi
  if await_health; then
    rm -f "$PENDING"
    echo "relay healthy on port $PORT"
    record_status restarted false
    exit 0
  fi
  echo "warning: relay did not answer /healthz within 15s of the restart; check the service logs" >&2
  echo "warning: the pending marker was kept, so the next run will retry the restart" >&2
  record_status failed true
  exit 1
fi

# 5. Defer: record the marker so a later run (scheduled or manual) completes
#    the restart.
printf '%s' "$NEW_HEAD" > "$PENDING"
echo "skipping restart ($STATE); the next run will complete it, or rerun with --force"
record_status updated true
exit 0
