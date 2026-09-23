---
name: agent-relay
description: Delegate a task from one coding agent to another. Use when the user wants cross-harness delegation, fan-out across agents, or agent chaining. Covers relay setup, registering agents, starting HTTP/MCP, and submitting, polling, and cancelling tasks.
---

# Agent Relay

`agent-relay` is a small local service (Node.js 22+, zero dependencies) that lets one
agent hand a **new** task to another agent, then inspect, poll, or cancel it. The relay
owns task IDs, queueing, status, timeouts, cancellation, idempotency, and a registry of
adapters. It speaks HTTP as its core transport and exposes the same service as MCP tools
for orchestrating agents.

```
Orchestrator (Claude Code / Codex / Pi / OpenCode / ...)
        | MCP tools or HTTP
        v
  agent-relay HTTP task service   (registry + queue + task state)
        |
  command | stdio | http adapter
        v
  target coding agent
```

## When to use this skill

Use it when the user wants to:

- have one coding agent delegate a task to a different coding agent or harness;
- run the same job on several agents and compare results;
- chain agents (A writes → B reviews → C fixes);
- wrap a harness that has no convenient CLI behind a small adapter;
- give an MCP client the tools `list_agents`, `delegate`, `get_task`, `cancel_task`.

## Prerequisites

- Node.js 22 or newer (`node -v`).
- At least one target agent CLI installed and authenticated (or an HTTP/stdio adapter).
- Loopback only by default: no auth, no TLS. Do not bind to a public interface.

## 1. Get the source

Run the bundled setup script (idempotent):

```bash
bash scripts/setup.sh
```

It verifies Node, locates an existing checkout or clones the repo, copies
`config/agents.example.json` to `config/agents.json` if missing, and runs a self-test.
Override with env vars: `AGENT_RELAY_SOURCE` (use an existing checkout),
`AGENT_RELAY_DIR` (clone target, default `agent-relay`), `AGENT_RELAY_REPO` (git URL).

To do it manually:

```bash
git clone https://github.com/Ami-Khokhar/agent-relay.git && cd agent-relay
cp config/agents.example.json config/agents.json
```

## 2. Write the agent registry

Edit `config/agents.json`. Each entry is one target agent. Choose the adapter by how the
harness is invoked:

| Adapter | Use when | Required fields |
| --- | --- | --- |
| `command` | CLI accepts a prompt as its **final argument** and prints the result | `command`, `args[]` |
| `stdio` | You can run a wrapper that reads one JSON request on stdin and writes one JSON result on stdout | `command`, `args[]` |
| `http` | The harness (or a wrapper) is an HTTP endpoint returning the result JSON | `url` |

Common harnesses (verify flags against the installed version first):

```json
{
  "agents": [
    { "id": "claude",   "name": "Claude Code", "command": "claude",   "args": ["--print"], "cwd": "/path/to/project" },
    { "id": "codex",    "name": "Codex",       "command": "codex",    "args": ["exec"],    "cwd": "/path/to/git/project" },
    { "id": "pi",       "name": "Pi",          "command": "pi",       "args": ["--print"], "cwd": "/path/to/project" },
    { "id": "opencode", "name": "OpenCode",    "command": "opencode", "args": ["run"],     "cwd": "/path/to/project" },
    { "id": "dsh",      "name": "DeepSeek",    "command": "dsh",      "args": ["--profile", "headless"] },
    { "id": "wrapped",  "name": "Any harness", "type": "stdio", "command": "node", "args": ["/abs/adapter.mjs"], "cwd": "/path/to/project" },
    { "id": "hosted",   "name": "Hosted",      "type": "http",  "url": "http://127.0.0.1:9000/run" }
  ]
}
```

Per-agent optional fields: `description`, `cwd`, `env` (extra env for the child),
`inheritEnv` (names of relay env vars to pass through, e.g. credentials), `timeoutMs`.
`capabilities` may only declare what is true; the relay rejects `nativeSessions: true`,
`streaming: true`, and `newTasks: false` in this version.

**Security:** command/stdio agents inherit only basic env vars (`PATH`, `HOME`, `USER`,
`SHELL`, `TMPDIR`, `LANG`, `LC_ALL`, Windows equivalents). To pass a credential, list its
name in that agent's `inheritEnv` — do not inline secrets in the registry.

## 3. Start the relay

```bash
npm start          # HTTP API on http://127.0.0.1:43124  (or: node src/server.mjs)
npm run start:mcp  # MCP stdio server, in a second process (or: node src/mcp-server.mjs)
```

The HTTP service is the core; MCP is a thin client over it, so start HTTP first. `SIGTERM`
or `SIGINT` cancels running tasks and waits briefly for adapters to stop.

Register the MCP server in the orchestrating client (point `args` at the absolute path):

```json
{ "mcpServers": { "agent-relay": {
  "command": "node",
  "args": ["/abs/path/to/agent-relay/src/mcp-server.mjs"],
  "env": { "A2A_RELAY_URL": "http://127.0.0.1:43124" }
} } }
```

## 4. Verify

```bash
curl -sS http://127.0.0.1:43124/healthz
curl -sS http://127.0.0.1:43124/v1/agents
```

`/v1/agents` should list your entries with `adapter` and `capabilities`. If it is empty,
the registry did not parse — the relay prints `Configuration error: ...` and exits on a
bad registry, so check the server log.

## 5. Delegate work

**Via HTTP** (submit returns `202` immediately; then poll):

```bash
TASK=$(curl -sS http://127.0.0.1:43124/v1/tasks \
  -H 'content-type: application/json' \
  -d '{"agentId":"claude","requestId":"review-1","input":"Inspect this repo and list the top 3 risks"}')
echo "$TASK"
ID=$(printf '%s' "$TASK" | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>console.log(JSON.parse(s).id))')
curl -sS "http://127.0.0.1:43124/v1/tasks/$ID"
curl -sS -X DELETE "http://127.0.0.1:43124/v1/tasks/$ID"   # cancel
```

**Via MCP** tools:

- `list_agents {}` → registered agents.
- `delegate { agentId, input, sessionId?, requestId?, timeoutMs? }` → task.
- `get_task { taskId }` → current task state and result.
- `cancel_task { taskId }` → cancel a queued or running task.

**Always poll `get_task` until the status is terminal** — there is no push notification.

## Task lifecycle

Statuses: `queued` → `running` → one of `completed`, `failed`, `timed_out`, `cancelled`.

Result fields on a terminal task: `output` (success text), `error` (failure reason),
`outputTruncated` (bool). `sessionId` is **correlation data only** — it does not resume a
native harness session, and each task is a fresh invocation.

Idempotency: send a unique `requestId`; reusing it with the same fields returns the
original task (`200`), while reusing it with different fields returns `409`. Use it when a
submission response may have been lost. Without a `requestId`, do not auto-retry.

## Designing for the user's use case

1. **Identify the target agent(s)** the user means, and confirm their CLIs exist (`which`).
2. **Pick the adapter**: `command` for a simple prompt-as-final-arg CLI; `stdio`/`http`
   when the harness needs a wrapper or returns structured data.
3. **Register** them in `config/agents.json`, set `cwd` to the project each should edit.
4. **Choose limits**: `timeoutMs` per task/agent, and the global concurrency via
   `A2A_RELAY_MAX_ACTIVE` (default 4; extra tasks queue).
5. **Submit, poll, and present** the `output`/`error` to the user. Cancel if they change
   their mind. For multi-agent patterns and worked examples, read
   [references/use-cases.md](references/use-cases.md).

Because agents can themselves be MCP clients, you can chain them: A delegates to B, B
delegates to C. The relay does not model a cross-agent conversation, so thread continuity
yourself with a shared `sessionId` value used as a correlation tag.

## Adapter contract (for custom harnesses)

A `stdio` or JSON `http` adapter receives exactly one request document:

```json
{"protocolVersion":"relay.adapter/v1","task":{"id":"relay-task-id","sessionId":"correlation-id","input":"do the work","timeoutMs":120000}}
```

and returns exactly one result document:

```json
{"protocolVersion":"relay.adapter/v1","status":"completed","output":"result text"}
{"protocolVersion":"relay.adapter/v1","status":"failed","error":"reason"}
```

A stdio adapter reads the request from stdin, writes only the result to stdout, and sends
diagnostics to stderr. Copy `examples/stdio-adapter.mjs` and replace `runHarness`. The
relay rejects malformed envelopes, non-string `output`/`error`, and failures without an
`error`. See [references/adapters.md](references/adapters.md) for full rules and a template.

## Operating limits and security

- Binds to loopback (`A2A_RELAY_HOST`, default `127.0.0.1`). Setting a non-loopback host
  exposes an unauthenticated service that can run your coding agents — never do it without
  an authenticated HTTPS boundary.
- Tasks are in memory and disappear on restart; old terminal tasks are evicted when the
  store fills. There is no list-tasks endpoint: keep task IDs.
- Limits (all positive integers): `A2A_RELAY_MAX_BODY_BYTES` (1 MiB),
  `A2A_RELAY_MAX_COMMAND_INPUT_BYTES` (64 KiB, command adapter only),
  `A2A_RELAY_MAX_OUTPUT_BYTES` (256 KiB), `A2A_RELAY_MAX_TASKS` (1000),
  `A2A_RELAY_MAX_ACTIVE` (4), `A2A_RELAY_TIMEOUT_MS` (120000).
- Cancellation of a spawned adapter sends process signals; cancellation of an HTTP adapter
  aborts the request and may not stop work already accepted by that service.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Relay exits with `Configuration error: ...` | Invalid `config/agents.json`; the message names the agent. |
| `404 unknown_agent` | `agentId` not in the registry, or registry changed without restart. |
| `413 command_input_too_large` | Input exceeds `A2A_RELAY_MAX_COMMAND_INPUT_BYTES`; use a `stdio` adapter for large prompts. |
| `503 task_capacity_reached` | Store full of non-terminal tasks; raise `A2A_RELAY_MAX_TASKS`/`A2A_RELAY_MAX_ACTIVE` or wait. |
| `409 idempotency_conflict` | Same `requestId` reused with different fields; use a new ID. |
| Task `failed` with adapter errors | Harness exit non-zero or bad output; check stderr by running the command adapter manually. |
| Task `timed_out` | Exceeded `timeoutMs`; raise it or shorten the work. |
| MCP tool returns `isError: true` | Relay unreachable or returned an error; the payload carries `error`/`status`. |

## Reference

- [references/api.md](references/api.md) — full HTTP and MCP surface, fields, and errors.
- [references/adapters.md](references/adapters.md) — adapter contract, template, env vars.
- [references/use-cases.md](references/use-cases.md) — single delegation, fan-out, pipelines.
- [scripts/setup.sh](scripts/setup.sh) — idempotent setup.
