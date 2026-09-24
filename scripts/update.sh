#!/usr/bin/env bash
# Update an installed agent-relay checkout and restart its service when the
# code changed.
#
# Safe to run repeatedly: when HEAD already matches the default branch nothing
# is restarted, unless a previous run was deferred by the busy guard — that
# pending restart is then completed. Because tasks and sessions live in memory,
# the restart is skipped while tasks are queued or running unless --force is
# given. A relay that does not report active/queued counts (pre-feature code)
# is treated as busy, so an old relay is never killed by a scheduled update.
#
# Usage:
#   scripts/update.sh [--force] [--no-restart]
#
# Env overrides (same as setup.sh):
#   AGENT_RELAY_DIR   install directory (default: ~/.local/share/agent-relay)
#   AGENT_RELAY_REPO  git URL, only used when the checkout is missing
#   A2A_RELAY_PORT    relay port for the health check (default: 43124)
set -euo pipefail

TARGET_DIR="${AGENT_RELAY_DIR:-$HOME/.local/share/agent-relay}"
REPO_URL="${AGENT_RELAY_REPO:-https://github.com/Ami-Khokhar/agent-relay.git}"
PORT="${A2A_RELAY_PORT:-${AGENT_RELAY_PORT:-43124}}"
LABEL="com.agent-relay"
PENDING="$TARGET_DIR/.git/agent-relay-pending-restart"
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

# busy: 0 = idle (health reports active/queued and both are 0), 1 = busy or
# unknown. Unknown counts fail closed so an old relay is never killed.
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
#    earlier busy run instead of exiting: the code is already new, only the
#    running process is old.
OLD_HEAD="$(git -C "$TARGET_DIR" rev-parse HEAD)"
if ! git -C "$TARGET_DIR" pull --ff-only origin main; then
  echo "warning: could not fast-forward to main; the checkout may need manual attention" >&2
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
    exit 0
  fi
else
  echo "updated: $OLD_HEAD -> $NEW_HEAD"
fi

# 3. Restart the service so the running relay picks up the new code. Tasks and
#    sessions are in memory and die with the process, so skip the restart while
#    work is in flight (or counts are unknown) unless --force was given. The
#    skip is remembered so the next run completes it.
STATE="busy: $(busy)"
if [ "$STATE" = "busy: idle" ] || [ "$FORCE" -eq 1 ]; then
  if [ "$NO_RESTART" -eq 1 ]; then
    echo "--no-restart: leaving the running service alone ($STATE)"
    exit 0
  fi
  if restart_service; then
    rm -f "$PENDING"
    sleep 1
    if health | grep -q '"ok": *true'; then
      echo "relay healthy on port $PORT"
      exit 0
    fi
    echo "warning: relay did not answer /healthz after the restart; check the service logs" >&2
    exit 1
  fi
  exit 1
fi

REASON="$STATE"
[ "$FORCE" -ne 1 ] && [ "$NO_RESTART" -eq 0 ] && REASON="$STATE; rerun with --force to restart now"
if [ "$NO_RESTART" -eq 1 ]; then
  echo "--no-restart: leaving the running service alone ($STATE)"
  exit 0
fi
printf '%s' "$NEW_HEAD" > "$PENDING"
echo "skipping restart ($STATE); the next run will complete it, or rerun with --force"
exit 0
