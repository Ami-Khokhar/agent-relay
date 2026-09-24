#!/usr/bin/env bash
# Update an installed agent-relay checkout and restart its service when the
# code changed.
#
# Safe to run repeatedly: when HEAD already matches the default branch nothing
# is restarted. Because tasks and sessions live in memory, the restart is
# skipped while tasks are queued or running unless --force is given.
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
FORCE=0
NO_RESTART=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --no-restart) NO_RESTART=1 ;;
    *) echo "usage: $0 [--force] [--no-restart]" >&2; exit 2 ;;
  esac
done

# 1. Locate the checkout; bootstrap it when missing.
if [ ! -d "$TARGET_DIR/.git" ]; then
  echo "no checkout at $TARGET_DIR; running setup"
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  exec bash "$SCRIPT_DIR/setup.sh"
fi

OLD_HEAD="$(git -C "$TARGET_DIR" rev-parse HEAD)"
if ! git -C "$TARGET_DIR" pull --ff-only origin main; then
  echo "warning: could not fast-forward to main; the checkout may need manual attention" >&2
  exit 1
fi
NEW_HEAD="$(git -C "$TARGET_DIR" rev-parse HEAD)"

if [ "$OLD_HEAD" = "$NEW_HEAD" ]; then
  echo "already up to date ($NEW_HEAD)"
  exit 0
fi
echo "updated: $OLD_HEAD -> $NEW_HEAD"

# 2. Restart the service so the running relay picks up the new code. Tasks and
#    sessions are in memory and die with the process, so skip the restart while
#    work is in flight unless --force was given.
health() { curl -sS --max-time 2 "http://127.0.0.1:$PORT/healthz" 2>/dev/null || true; }

HEALTH="$(health)"
if [ -n "$HEALTH" ] && printf '%s' "$HEALTH" | grep -q '"ok": *true'; then
  BUSY="$(printf '%s' "$HEALTH" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("active", 0) + d.get("queued", 0))' 2>/dev/null || echo 0)"
  if [ "$BUSY" -gt 0 ] && [ "$FORCE" -ne 1 ]; then
    echo "skipping restart: $BUSY task(s) in flight; rerun with --force or after they finish"
    exit 0
  fi
  REASON="code changed"
  if [ "$BUSY" -gt 0 ]; then REASON="code changed (--force with $BUSY task(s) in flight)"; fi
else
  REASON="service not responding on $PORT"
fi

if [ "$NO_RESTART" -eq 1 ]; then
  echo "--no-restart: leaving the running service alone ($REASON)"
  exit 0
fi

echo "restarting service ($REASON)"
if [ "$(uname -s)" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  if [ -f "$PLIST" ]; then
    launchctl kickstart -k "gui/$(id -u)/$LABEL"
  else
    echo "warning: no $PLIST; start the relay manually or run scripts/install-service.sh" >&2
  fi
else
  if systemctl --user cat agent-relay.service >/dev/null 2>&1; then
    systemctl --user restart agent-relay.service
  else
    echo "warning: no agent-relay systemd user unit; start the relay manually or run scripts/install-service.sh" >&2
  fi
fi

# 3. Verify the restarted relay answers with the new code.
sleep 1
if health | grep -q '"ok": *true'; then
  echo "relay healthy on port $PORT"
else
  echo "warning: relay did not answer /healthz after the restart; check the service logs" >&2
  exit 1
fi
