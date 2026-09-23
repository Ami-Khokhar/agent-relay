#!/usr/bin/env bash
# Idempotent setup for agent-relay.
#
# Locates or clones the source, verifies Python 3.9+, scaffolds the agent registry
# from the example if missing, runs the self-test, and prints copy-paste MCP
# registration for each client.
#
# Env overrides:
#   AGENT_RELAY_SOURCE   use this existing checkout instead of cloning
#   AGENT_RELAY_DIR      install directory (default: ~/.local/share/agent-relay)
#   AGENT_RELAY_REPO     git URL (default: the project repo)
#   AGENT_RELAY_INSTALL_SKILL=1  link skills/agent-relay into agent skill dirs
#
# Flags:
#   --install-skill      same as AGENT_RELAY_INSTALL_SKILL=1
set -euo pipefail

REPO_URL="${AGENT_RELAY_REPO:-https://github.com/Ami-Khokhar/agent-relay.git}"
TARGET_DIR="${AGENT_RELAY_DIR:-$HOME/.local/share/agent-relay}"
INSTALL_SKILL="${AGENT_RELAY_INSTALL_SKILL:-0}"
for arg in "$@"; do
  case "$arg" in
    --install-skill) INSTALL_SKILL=1 ;;
    *) echo "warning: unknown argument: $arg" >&2 ;;
  esac
done

# 1. Python 3.9+
if ! command -v python3 >/dev/null 2>&1; then
  echo "error: Python 3.9 or newer is required but 'python3' was not found." >&2
  exit 1
fi
PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_MAJOR="${PY_VERSION%%.*}"
PY_MINOR="${PY_VERSION##*.}"
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]; }; then
  echo "error: Python 3.9 or newer is required; found $(python3 --version 2>&1)." >&2
  exit 1
fi
PYTHON="$(command -v python3)"
echo "python $PY_VERSION ok ($PYTHON)"

# 2. Locate or clone the source. The default target is a stable per-user path so running
#    the skill from an unrelated project does not clone into that project.
if [ -n "${AGENT_RELAY_SOURCE:-}" ]; then
  SRC="$AGENT_RELAY_SOURCE"
elif [ -f pyproject.toml ] && grep -q 'name = "agent-relay"' pyproject.toml; then
  SRC="$PWD"
elif [ -d "$TARGET_DIR/.git" ]; then
  SRC="$TARGET_DIR"
  echo "updating existing checkout at $SRC"
  git -C "$SRC" pull --ff-only || echo "warning: could not fast-forward; continuing"
else
  echo "cloning $REPO_URL into $TARGET_DIR"
  mkdir -p "$(dirname "$TARGET_DIR")"
  git clone --depth 1 "$REPO_URL" "$TARGET_DIR"
  SRC="$TARGET_DIR"
fi
SRC="$(cd "$SRC" && pwd)"
echo "source: $SRC"

if [ ! -f "$SRC/src/server.py" ] || [ ! -f "$SRC/src/mcp_server.py" ]; then
  echo "error: $SRC does not look like an agent-relay checkout." >&2
  exit 1
fi

# 3. Scaffold the registry in the per-user config directory when the checkout has none.
USER_CONFIG_DIR="$HOME/.config/agent-relay"
USER_CONFIG="$USER_CONFIG_DIR/agents.json"
if [ ! -f "$SRC/config/agents.json" ] && [ ! -f "$USER_CONFIG" ]; then
  mkdir -p "$USER_CONFIG_DIR"
  cp "$SRC/config/agents.example.json" "$USER_CONFIG"
  echo "created $USER_CONFIG from the example (edit it before starting)"
elif [ -f "$USER_CONFIG" ]; then
  echo "registry present: $USER_CONFIG"
else
  echo "registry present: $SRC/config/agents.json"
fi

# 4. Self-test (non-fatal)
if ( cd "$SRC" && python3 -W ignore::ResourceWarning -m unittest discover -s test -p 'test_*.py' >/dev/null 2>&1 ); then
  echo "self-test passed"
else
  echo "warning: self-test did not pass; inspect with: cd \"$SRC\" && python3 -m unittest discover -s test -p 'test_*.py'"
fi

# 5. Optionally link the skill into each client's skill directory.
if [ "$INSTALL_SKILL" = "1" ]; then
  for SKILL_ROOT in "$HOME/.claude/skills" "$HOME/.codex/skills" "$HOME/.agents/skills"; do
    mkdir -p "$SKILL_ROOT"
    LINK="$SKILL_ROOT/agent-relay"
    if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
      echo "warning: $LINK exists and is not a symlink; skipping"
      continue
    fi
    ln -sfn "$SRC/skills/agent-relay" "$LINK"
    echo "linked skill: $LINK -> $SRC/skills/agent-relay"
  done
fi

MCP_CMD="$PYTHON $SRC/src/mcp_server.py"

cat <<EOF

Next steps:
  # 1. edit the registry to register your target harnesses:
  #      $USER_CONFIG
  #    (or $SRC/config/agents.json)
  # 2. start the HTTP service (core):
  python3 $SRC/src/server.py
  # 3. keep it running across reboots:
  bash $SRC/scripts/install-service.sh

Register the MCP server (absolute paths; HTTP starts on demand):
  Claude Code:
    claude mcp add --scope user agent-relay -e A2A_RELAY_URL=http://127.0.0.1:43124 -- $MCP_CMD
  Codex (~/.codex/config.toml):
    [mcp_servers.agent-relay]
    command = "$PYTHON"
    args = ["$SRC/src/mcp_server.py"]
    env = { A2A_RELAY_URL = "http://127.0.0.1:43124" }
  OpenCode (opencode.json):
    { "mcp": { "agent-relay": { "type": "local", "command": ["$PYTHON", "$SRC/src/mcp_server.py"],
      "environment": { "A2A_RELAY_URL": "http://127.0.0.1:43124" }, "enabled": true } } }
  Pi / generic JSON:
    { "mcpServers": { "agent-relay": { "command": "$PYTHON",
      "args": ["$SRC/src/mcp_server.py"], "env": { "A2A_RELAY_URL": "http://127.0.0.1:43124" } } } }

Verify:
  curl -sS http://127.0.0.1:43124/healthz
  bash $SRC/scripts/smoke.sh <agentId>
EOF
