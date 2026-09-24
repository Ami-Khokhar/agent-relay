#!/usr/bin/env bash
# Install agent-relay's HTTP service as a per-user service.
#
# macOS: ~/Library/LaunchAgents/com.agent-relay.plist (launchd)
# Linux: ~/.config/systemd/user/agent-relay.service (systemd --user)
#
# Usage:
#   scripts/install-service.sh            install and start
#   scripts/install-service.sh --with-update-timer
#                                   also install a scheduled update that runs
#                                   scripts/update.sh every 6 hours
#   scripts/install-service.sh --uninstall [--with-update-timer]
#
# Env:
#   AGENT_RELAY_DIR      source checkout (default ~/.local/share/agent-relay)
#   AGENT_RELAY_PYTHON   python interpreter (default: the python3 on PATH)
#   A2A_AGENTS_FILE      registry path (default: the checkout's config/agents.json)
#   A2A_RELAY_PORT       listen port (default 43124)
#   AGENT_RELAY_PATH     extra PATH entries for the service (colon separated)
#   AGENT_RELAY_UPDATE_INTERVAL_SECS  timer period (default 21600 = 6 h)
set -euo pipefail

TARGET_DIR="${AGENT_RELAY_DIR:-$HOME/.local/share/agent-relay}"
UPDATE_LABEL="com.agent-relay.update"
UPDATE_INTERVAL="${AGENT_RELAY_UPDATE_INTERVAL_SECS:-21600}"
PYTHON="${AGENT_RELAY_PYTHON:-$(command -v python3 || true)}"
PORT="${A2A_RELAY_PORT:-${AGENT_RELAY_PORT:-43124}}"
LABEL="com.agent-relay"
LOG_DIR="$HOME/.local/state/agent-relay"

WITH_UPDATE_TIMER=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --with-update-timer) WITH_UPDATE_TIMER=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *) echo "warning: unknown argument: $arg" >&2 ;;
  esac
done

if [ "$UNINSTALL" = "1" ]; then
  if [ "$(uname -s)" = "Darwin" ]; then
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed $PLIST"
    UPDATE_PLIST="$HOME/Library/LaunchAgents/$UPDATE_LABEL.plist"
    if [ -f "$UPDATE_PLIST" ]; then
      launchctl unload "$UPDATE_PLIST" 2>/dev/null || true
      rm -f "$UPDATE_PLIST"
      echo "removed $UPDATE_PLIST"
    fi
  else
    UNIT="$HOME/.config/systemd/user/agent-relay.service"
    systemctl --user disable --now agent-relay.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "removed $UNIT"
    systemctl --user disable --now agent-relay-update.timer 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/agent-relay-update.service" \
          "$HOME/.config/systemd/user/agent-relay-update.timer"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "removed agent-relay-update.timer"
  fi
  exit 0
fi

if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
  echo "error: python3 not found; set AGENT_RELAY_PYTHON to an absolute path" >&2
  exit 1
fi
if [ ! -f "$TARGET_DIR/src/server.py" ]; then
  echo "error: $TARGET_DIR does not look like an agent-relay checkout" >&2
  echo "       set AGENT_RELAY_DIR or run scripts/setup.sh first" >&2
  exit 1
fi

# Services do not inherit the login shell PATH. Include the usual per-user tool dirs so
# agent CLIs (codex, claude, pi, opencode, dsh) resolve.
SERVICE_PATH="$HOME/.local/bin:$HOME/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if [ -n "${AGENT_RELAY_PATH:-}" ]; then
  SERVICE_PATH="$AGENT_RELAY_PATH:$SERVICE_PATH"
fi

mkdir -p "$LOG_DIR"

if [ "$(uname -s)" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  mkdir -p "$(dirname "$PLIST")"
  cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$TARGET_DIR/src/server.py</string>
  </array>
  <key>WorkingDirectory</key><string>$TARGET_DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$SERVICE_PATH</string>
    <key>A2A_RELAY_PORT</key><string>$PORT</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/relay.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/relay.log</string>
</dict>
</plist>
PLIST_EOF
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  echo "installed and started $PLIST"
  echo "log: $LOG_DIR/relay.log"
else
  UNIT="$HOME/.config/systemd/user/agent-relay.service"
  mkdir -p "$(dirname "$UNIT")"
  cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=agent-relay local task service
After=network.target

[Service]
Type=simple
WorkingDirectory=$TARGET_DIR
Environment=PATH=$SERVICE_PATH
Environment=A2A_RELAY_PORT=$PORT
ExecStart=$PYTHON $TARGET_DIR/src/server.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
UNIT_EOF
  systemctl --user daemon-reload
  systemctl --user enable --now agent-relay.service
  echo "installed and started $UNIT"
  echo "log: journalctl --user -u agent-relay -f"
fi

echo "verify: curl -sS http://127.0.0.1:$PORT/healthz"

# Optional scheduled update: keep the checkout current and restart the service
# when the code changed. See scripts/update.sh.
if [ "$WITH_UPDATE_TIMER" = "1" ]; then
  if [ "$(uname -s)" = "Darwin" ]; then
    UPDATE_PLIST="$HOME/Library/LaunchAgents/$UPDATE_LABEL.plist"
    mkdir -p "$(dirname "$UPDATE_PLIST")" "$HOME/Library/Logs/agent-relay"
    cat > "$UPDATE_PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$UPDATE_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$TARGET_DIR/scripts/update.sh</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$SERVICE_PATH</string>
    <key>A2A_RELAY_PORT</key><string>$PORT</string>
    <key>AGENT_RELAY_DIR</key><string>$TARGET_DIR</string>
  </dict>
  <key>StartInterval</key><integer>$UPDATE_INTERVAL</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/agent-relay/update.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/agent-relay/update.log</string>
</dict>
</plist>
PLIST_EOF
    launchctl unload "$UPDATE_PLIST" 2>/dev/null || true
    launchctl load "$UPDATE_PLIST"
    echo "update timer installed: $UPDATE_PLIST (every ${UPDATE_INTERVAL}s)"
    echo "update log: $HOME/Library/Logs/agent-relay/update.log"
  else
    UPDATE_SERVICE="$HOME/.config/systemd/user/agent-relay-update.service"
    UPDATE_TIMER="$HOME/.config/systemd/user/agent-relay-update.timer"
    mkdir -p "$(dirname "$UPDATE_SERVICE")"
    cat > "$UPDATE_SERVICE" <<UNIT_EOF
[Unit]
Description=agent-relay scheduled update

[Service]
Type=oneshot
Environment=PATH=$SERVICE_PATH
Environment=A2A_RELAY_PORT=$PORT
Environment=AGENT_RELAY_DIR=$TARGET_DIR
ExecStart=/bin/bash $TARGET_DIR/scripts/update.sh
UNIT_EOF
    cat > "$UPDATE_TIMER" <<UNIT_EOF
[Unit]
Description=Run agent-relay update periodically

[Timer]
OnBootSec=10min
OnUnitActiveSec=$UPDATE_INTERVAL

[Install]
WantedBy=timers.target
UNIT_EOF
    systemctl --user daemon-reload
    systemctl --user enable --now agent-relay-update.timer
    echo "update timer installed: $UPDATE_TIMER (every ${UPDATE_INTERVAL}s)"
  fi
fi
