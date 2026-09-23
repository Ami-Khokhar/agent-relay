#!/usr/bin/env bash
# Idempotent setup for agent-relay.
#
# Locates or clones the source, verifies Node.js 22+, scaffolds the agent registry
# from the example if missing, and runs the self-test.
#
# Env overrides:
#   AGENT_RELAY_SOURCE  use this existing checkout instead of cloning
#   AGENT_RELAY_DIR     clone target directory (default: agent-relay)
#   AGENT_RELAY_REPO    git URL (default: the project repo)
set -euo pipefail

REPO_URL="${AGENT_RELAY_REPO:-https://github.com/Ami-Khokhar/agent-relay.git}"
TARGET_DIR="${AGENT_RELAY_DIR:-agent-relay}"

# 1. Node.js 22+
if ! command -v node >/dev/null 2>&1; then
  echo "error: Node.js 22 or newer is required but 'node' was not found." >&2
  exit 1
fi
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
if [ "$NODE_MAJOR" -lt 22 ]; then
  echo "error: Node.js 22 or newer is required; found $(node -v)." >&2
  exit 1
fi
echo "node $(node -v) ok"

# 2. Locate or clone the source
if [ -n "${AGENT_RELAY_SOURCE:-}" ]; then
  SRC="$AGENT_RELAY_SOURCE"
elif [ -f package.json ] && grep -q '"name": *"agent-relay"' package.json; then
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

if [ ! -f "$SRC/src/server.mjs" ] || [ ! -f "$SRC/src/mcp-server.mjs" ]; then
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
if ( cd "$SRC" && node --test "test/**/*.test.mjs" >/dev/null 2>&1 ); then
  echo "self-test passed"
else
  echo "warning: self-test did not pass; inspect with: cd \"$SRC\" && npm test"
fi

cat <<EOF

Next steps:
  cd "$SRC"
  # 1. edit config/agents.json to register your target harnesses
  # 2. start the HTTP service (core):
  node src/server.mjs
  # 3. optionally start the MCP stdio server in another process:
  node src/mcp-server.mjs

Verify:  curl -sS http://127.0.0.1:43124/v1/agents
EOF
