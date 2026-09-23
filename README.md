# Local agent relay

One agent can submit a task to another agent through a small local HTTP service. The relay owns task IDs, status, cancellation, timeouts, and a registry of adapters. An MCP stdio entry point exposes the same service as tools to orchestrating agents.

## Run

Node.js 22 or newer is required. There are no package dependencies.

```bash
npm start
```

The HTTP API listens on `http://127.0.0.1:43124` by default. Start the MCP interface in a second process when the orchestrating client supports MCP:

```bash
npm run start:mcp
```

Configure that command as a local stdio MCP server in the client. It provides `list_agents`, `delegate`, `get_task`, and `cancel_task`. `A2A_RELAY_URL` overrides the HTTP address it calls.

## Register an agent

Copy `config/agents.example.json` to `config/agents.json`, edit it, and restart the HTTP service. (`config/agents.json` is local-only and not committed; the example is the portable template.) Use a `stdio` or `http` adapter for harness-independent integration. A `command` adapter remains available as a convenience for CLIs that accept a prompt as their final argument and print the result.

```json
{
  "agents": [
    { "id": "claude", "name": "Claude Code", "command": "claude", "args": ["--print"], "cwd": "/path/to/project" },
    { "id": "codex", "name": "Codex", "command": "codex", "args": ["exec"], "cwd": "/path/to/git/project" },
    { "id": "pi", "name": "Pi", "command": "pi", "args": ["--print"], "cwd": "/path/to/project" },
    { "id": "opencode", "name": "OpenCode", "command": "opencode", "args": ["run"], "cwd": "/path/to/project" },
    { "id": "wrapped", "name": "Any wrapped harness", "type": "stdio", "command": "node", "args": ["/path/to/adapter.mjs"], "cwd": "/path/to/project" },
    { "id": "hosted", "name": "Harness on its own port", "type": "http", "url": "http://127.0.0.1:9000/run" }
  ]
}
```

The relay only starts new tasks; continuing an existing agent session needs a native adapter for that agent. Freebuff's current installed CLI does not advertise a noninteractive output mode, so it is not registered as an automatic command adapter.

`stdio` and JSON `http` adapters share one small, harness-neutral contract. The relay sends one document:

```json
{"protocolVersion":"relay.adapter/v1","task":{"id":"relay-task-id","sessionId":"correlation-id","input":"do the work","timeoutMs":120000}}
```

The adapter returns exactly one result document:

```json
{"protocolVersion":"relay.adapter/v1","status":"completed","output":"result text"}
```

For a harness failure, return `{"protocolVersion":"relay.adapter/v1","status":"failed","error":"reason"}`. A stdio adapter reads the request from stdin, writes only the result to stdout, and sends diagnostics to stderr. See [examples/stdio-adapter.mjs](examples/stdio-adapter.mjs); replace its `runHarness` function with the harness's supported API. This lets a new harness be added through config plus a small external wrapper, without changing relay or MCP code.

An HTTP adapter accepts the same request as a `POST` and returns the same result with `content-type: application/json`. It can run on a separate localhost port for each harness. The relay still gives MCP clients one stable HTTP port and handles the common queue and task lifecycle. Separate harness ports are optional and require running those adapter services. For compatibility, an HTTP adapter response without a JSON content type is still treated as a successful raw-text result when its status is 2xx.

The `sessionId` is relay correlation data and does not claim native harness session continuity. This version advertises `nativeSessions: false` and `streaming: false`. Cancellation of a spawned adapter sends process signals; cancellation of an HTTP adapter aborts the request, which may not stop work already accepted by that service. `/v1/agents` reports these as `process_signal` and `request_only` respectively.

## Submit and inspect work

```bash
curl -sS http://127.0.0.1:43124/v1/agents
curl -sS http://127.0.0.1:43124/v1/tasks \
  -H 'content-type: application/json' \
  -d '{"agentId":"deepseek-harness","requestId":"review-2026-09-23-1","input":"Inspect this project and report the main risks"}'
curl -sS http://127.0.0.1:43124/v1/tasks/TASK_ID
curl -sS -X DELETE http://127.0.0.1:43124/v1/tasks/TASK_ID
```

Submission returns `202` with a task ID and session ID. Poll the task URL for `queued`, `running`, `completed`, `failed`, `timed_out`, or `cancelled`. Supply a unique `requestId` and reuse it when retrying a submission; the relay returns the original task instead of starting duplicate work. Reusing it with different task fields returns `409`. Without a request ID, callers must not automatically retry a submission whose response was lost. The session ID currently groups work for the caller; generic command adapters do not continue native agent conversations. No real agent is called by the tests.

## Scope and operation

HTTP is the core transport because callers can use it locally or across machines and do not need to share a runtime. MCP stdio is a client interface for agents that discover tools. A2A can be added as a protocol adapter if an A2A client is required. The former A2A shaped endpoint is not part of this HTTP task API.

The relay binds to loopback (`A2A_RELAY_HOST`, default `127.0.0.1`) and has no authentication or TLS. Setting `A2A_RELAY_HOST` to a non-loopback address exposes the API to the network; do not do so without an authenticated HTTPS boundary, because any client allowed to submit tasks can cause the configured coding agents to run. The registry file is `config/agents.json` by default and can be overridden with `A2A_AGENTS_FILE`. Tasks are held in memory and disappear on restart. At most four tasks run at once by default (`A2A_RELAY_MAX_ACTIVE`); the rest queue. Results are bounded, and completed tasks may be evicted when the task store fills. For a durable single-host deployment, add SQLite task storage. Command adapters inherit only basic process variables by default. If an adapter needs a credential already in the relay's environment, list its name in that agent's `inheritEnv` array rather than putting the secret value in the registry file. Cancellation stops the direct command process; child processes it started may require adapter-specific cleanup. On `SIGTERM` or `SIGINT` the relay cancels running tasks and waits briefly for adapter processes to terminate before exiting.

Resource limits can be changed with `A2A_RELAY_MAX_BODY_BYTES`, `A2A_RELAY_MAX_COMMAND_INPUT_BYTES`, `A2A_RELAY_MAX_OUTPUT_BYTES`, `A2A_RELAY_MAX_TASKS`, `A2A_RELAY_MAX_ACTIVE`, and `A2A_RELAY_TIMEOUT_MS`. Each must be a positive integer. Command input defaults to 64 KiB because it is passed as one argument and operating systems cap the combined argument and environment size. The output limit applies to the combined stdout and stderr captured from a command and to the body read from an HTTP adapter. The listener also honors `A2A_RELAY_PORT` (default `43124`) and `A2A_RELAY_HOST`; the MCP process honors `A2A_RELAY_URL` and `A2A_RELAY_HTTP_TIMEOUT_MS`.

Run `npm test` to exercise the relay and MCP interface with fake agents.
