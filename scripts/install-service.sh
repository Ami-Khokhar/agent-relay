#!/usr/bin/env bash
# Install agent-relay's HTTP service as a per-user service.
#
# macOS: ~/Library/LaunchAgents/com.agent-relay.plist (launchd)
# Linux: ~/.config/systemd/user/agent-relay.service (systemd --user)
#
# Usage:
#   scripts/install-service.sh            install and start
#   scripts/install-service.sh --uninstall
#
# Env:
#   AGENT_RELAY_DIR      source checkout (default ~/.local/share/agent-relay)
#   AGENT_RELAY_PYTHON   python interpreter (default: the python3 on PATH)
#   A2A_AGENTS_FILE      registry path (default: the checkout's config/agents.json)
#   A2A_RELAY_PORT       listen port (default 43124)
#   AGENT_RELAY_PATH     extra PATH entries for the service (colon separated)
set -euo pipefail

TARGET_DIR="${AGENT_RELAY_DIR:-$HOME/.local/share/agent-relay}"
PYTHON="${AGENT_RELAY_PYTHON:-$(command -v python3 || true)}"
PORT="${A2A_RELAY_PORT:-${AGENT_RELAY_PORT:-43124}}"
LABEL="com.agent-relay"
LOG_DIR="$HOME/.local/state/agent-relay"

if [ "${1:-}" = "--uninstall" ]; then
  if [ "$(uname -s)" = "Darwin" ]; then
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed $PLIST"
  else
    UNIT="$HOME/.config/systemd/user/agent-relay.service"
    systemctl --user disable --now agent-relay.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "removed $UNIT"
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
