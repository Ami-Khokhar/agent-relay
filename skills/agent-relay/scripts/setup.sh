#!/usr/bin/env bash
# Idempotent setup for agent-relay.
#
# Locates or clones the source, verifies Python 3.9+, scaffolds the agent registry
# from the example if missing, and runs the self-test.
#
# Env overrides:
#   AGENT_RELAY_SOURCE  use this existing checkout instead of cloning
#   AGENT_RELAY_DIR     clone target directory (default: agent-relay)
#   AGENT_RELAY_REPO    git URL (default: the project repo)
set -euo pipefail

REPO_URL="${AGENT_RELAY_REPO:-https://github.com/Ami-Khokhar/agent-relay.git}"
TARGET_DIR="${AGENT_RELAY_DIR:-agent-relay}"

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
echo "python $PY_VERSION ok"

# 2. Locate or clone the source
if [ -n "${AGENT_RELAY_SOURCE:-}" ]; then
  SRC="$AGENT_RELAY_SOURCE"
elif [ -f pyproject.toml ] && grep -q 'name = "agent-relay"' pyproject.toml; then
  SRC="$PWD"
elif [ -d "$TARGET_DIR/.git" ]; then
  SRC="$PWD/$TARGET_DIR"
  echo "updating existing checkout at $SRC"
  git -C "$SRC" pull --ff-only || echo "warning: could not fast-forward; continuing"
else
  echo "cloning $REPO_URL"
  git clone --depth 1 "$REPO_URL" "$TARGET_DIR"
  SRC="$PWD/$TARGET_DIR"
fi
SRC="$(cd "$SRC" && pwd)"
echo "source: $SRC"

if [ ! -f "$SRC/src/server.py" ] || [ ! -f "$SRC/src/mcp_server.py" ]; then
  echo "error: $SRC does not look like an agent-relay checkout." >&2
  exit 1
fi

# 3. Scaffold the registry
if [ ! -f "$SRC/config/agents.json" ]; then
  cp "$SRC/config/agents.example.json" "$SRC/config/agents.json"
  echo "created $SRC/config/agents.json from the example (edit it before starting)"
else
  echo "registry present: $SRC/config/agents.json"
fi

# 4. Self-test (non-fatal)
if ( cd "$SRC" && python3 -W ignore::ResourceWarning -m unittest discover -s test -p 'test_*.py' >/dev/null 2>&1 ); then
  echo "self-test passed"
else
  echo "warning: self-test did not pass; inspect with: cd \"$SRC\" && python3 -m unittest discover -s test -p 'test_*.py'"
fi

cat <<EOF

Next steps:
  cd "$SRC"
  # 1. edit config/agents.json to register your target harnesses
  # 2. start the HTTP service (core):
  python3 src/server.py
  # 3. optionally start the MCP stdio server in another process:
  python3 src/mcp_server.py

Verify:  curl -sS http://127.0.0.1:43124/v1/agents
EOF
