# agent-relay API reference

The HTTP service is the source of truth. MCP tools are a thin proxy over it.

## HTTP endpoints

### `GET /healthz`

```json
{ "ok": true, "agents": 3, "tasks": 0 }
```

### `GET /v1/agents`

```json
{ "agents": [
  { "id": "claude", "name": "Claude Code", "description": "...",
    "adapter": "command",
    "capabilities": { "newTasks": true, "nativeSessions": false, "streaming": false,
                      "cancellation": "process_signal" } }
] }
```

`cancellation` is `process_signal` for command/stdio adapters and `request_only` for HTTP.

### `POST /v1/tasks`

Body:

| Field | Required | Notes |
| --- | --- | --- |
| `agentId` | yes | Must match a registry id. |
| `input` | yes | Non-empty string; bounded by body and (for `command`) command-input limits. |
| `sessionId` | no | ≤128 chars; correlation tag. Defaults to a new UUID. |
| `requestId` | no | ≤128 chars; idempotency key scoped per agent. |
| `timeoutMs` | no | Positive integer; capped by the global timeout. |

Returns `202` with the task:

```json
{ "id": "uuid", "sessionId": "uuid", "agentId": "claude", "input": "...",
  "status": "queued", "createdAt": "ISO-8601", "timeoutMs": 120000, "requestId": "..." }
```

Idempotent replay with the same `requestId` and identical fields returns `200` with the
original task. Different fields with the same `requestId` return `409`.

### `GET /v1/tasks/:id`

Returns the current task. Terminal tasks additionally include `startedAt`, `finishedAt`,
and usually `output` / `error` / `outputTruncated`.

### `DELETE /v1/tasks/:id`

Cancels a `queued` or `running` task and returns it with status `cancelled`. Terminal
tasks are returned unchanged.

## Status values

`queued`, `running`, `completed`, `failed`, `timed_out`, `cancelled`.

## Error responses

| Status | `error` | Meaning |
| --- | --- | --- |
| 400 | `invalid_json`, `invalid_request`, `input_required`, `invalid_session_id`, `invalid_request_id`, `invalid_timeout` | Malformed request. |
| 404 | `unknown_agent`, `unknown_task`, `not_found` | Missing agent/task/route. |
| 405 | `method_not_allowed` | Known route, wrong method. |
| 409 | `idempotency_conflict` | `requestId` reused with different fields. |
| 413 | `command_input_too_large` (and body-too-large) | Input/body exceeds a limit. |
| 500 | `request_failed` | Unexpected server error (includes `message`). |
| 503 | `task_capacity_reached` | Store full of non-terminal tasks. |

## MCP tools

The MCP server (JSON-RPC 2.0 over stdio, one JSON object per line) implements:

- `initialize` → `{ protocolVersion: "2025-06-18", capabilities, serverInfo }`
- `tools/list` → the four tools below
- `ping`
- `tools/call`

| Tool | Arguments | Result |
| --- | --- | --- |
| `list_agents` | `{}` | Same payload as `GET /v1/agents`. |
| `delegate` | `{ agentId, input, sessionId?, requestId?, timeoutMs? }` | Submitted task. |
| `get_task` | `{ taskId }` | Current task. |
| `cancel_task` | `{ taskId }` | Cancelled task. |

Each result is returned as `content: [{ type: "text", text: "<json>" }]` plus
`structuredContent` holding the same object. Relay errors come back as `isError: true`
with `structuredContent` such as `{ "error": "unknown_task", "message": "...", "status": 404 }`.
Invalid tool arguments raise JSON-RPC error `-32602` before any HTTP call.

## Task object fields

| Field | When | Notes |
| --- | --- | --- |
| `id` | always | Task UUID. |
| `sessionId` | always | Correlation only; not a native harness session. |
| `agentId` | always | Registry id. |
| `input` | always | The submitted prompt. |
| `status` | always | See status values. |
| `createdAt` | always | ISO-8601. |
| `timeoutMs` | always | Effective timeout. |
| `requestId` | if supplied | Echoed idempotency key. |
| `startedAt` | running+ | ISO-8601. |
| `finishedAt` | terminal | ISO-8601. |
| `output` | success | Captured result text. |
| `error` | failure/timeout | Failure reason. |
