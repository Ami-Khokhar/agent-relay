#!/usr/bin/env bash
# Submit a "Reply with exactly PONG" probe to a registered agent and print the result.
#
# Usage: scripts/smoke.sh <agentId> [timeoutMs]
#
# Env:
#   A2A_RELAY_URL / AGENT_RELAY_URL   relay base URL (default http://127.0.0.1:43124)
set -euo pipefail

AGENT_ID="${1:-}"
TIMEOUT_MS="${2:-120000}"
RELAY_URL="${AGENT_RELAY_URL:-${A2A_RELAY_URL:-http://127.0.0.1:43124}}"
RELAY_URL="${RELAY_URL%/}"

if [ -z "$AGENT_ID" ]; then
  echo "usage: scripts/smoke.sh <agentId> [timeoutMs]" >&2
  exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is required" >&2
  exit 1
fi

REQUEST_ID="smoke-$(date +%s)-$$"

echo "relay:    $RELAY_URL"
echo "agent:    $AGENT_ID"
echo "probe:    Reply with exactly PONG"

SUBMITTED="$(curl -sS "$RELAY_URL/v1/tasks" \
  -H 'content-type: application/json' \
  -d "{\"agentId\":\"$AGENT_ID\",\"requestId\":\"$REQUEST_ID\",\"input\":\"Reply with exactly PONG\",\"timeoutMs\":$TIMEOUT_MS}")"

TASK_ID="$(printf '%s' "$SUBMITTED" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))')"
if [ -z "$TASK_ID" ]; then
  echo "error: relay did not return a task id: $SUBMITTED" >&2
  exit 1
fi
echo "task:     $TASK_ID"

# Long-poll until the task is terminal (or the relay's max wait elapses), then print it.
RESULT="$(curl -sS "$RELAY_URL/v1/tasks/$TASK_ID?waitMs=$TIMEOUT_MS")"

printf '%s' "$RESULT" | python3 -c '
import json, sys
task = json.load(sys.stdin)
print("status:  ", task.get("status"))
if task.get("output"):
    print("output:  ", task["output"].strip())
if task.get("error"):
    print("error:   ", task["error"])
if task.get("outputTruncated"):
    print("note:     output was truncated")
sys.exit(0 if task.get("status") == "completed" else 1)
'
