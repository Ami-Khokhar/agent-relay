# agent-relay API reference

The HTTP service is the source of truth. MCP tools are a thin proxy over it.

Every `/v1/` route requires `Authorization: Bearer <token>`. The token is
`AGENT_RELAY_TOKEN`, else the file `~/.config/agent-relay/token` (override with
`AGENT_RELAY_TOKEN_FILE`), which the relay creates with mode `0600` on first start. Requests
carrying an `Origin` header are refused unless that origin is listed in
`AGENT_RELAY_ALLOWED_ORIGINS` (meant for non-browser clients; the relay sends no CORS
headers). Error responses close the connection. `GET /healthz` needs no token.

## HTTP endpoints

### `GET /healthz`

```json
{ "ok": true, "agents": 3, "tasks": 0, "active": 0, "queued": 0,
  "limits": { "timeoutMs": 900000, "maxTimeoutMs": null, "maxWaitMs": 600000,
              "maxBodyBytes": 1048576, "maxCommandInputBytes": 65536,
              "maxOutputBytes": 262144, "maxTasks": 1000, "maxActive": 4,
              "taskRetentionMs": null, "maxDelegationDepth": 2, "maxDelegatedTasks": 20 } }
```

`maxTimeoutMs` is `null` when no hard cap is configured, and `taskRetentionMs` is `null` when
no retention period is set. `active` counts running tasks
and `queued` counts waiting tasks (the same distinction as the task statuses;
`scripts/update.sh` defers the first restart while either is non-zero, and treats a relay
that does not report them as busy on the first pass — a relay still unreadable on the
next run is restarted, since an old relay cannot report counts).

### `GET /v1/agents`

```json
{ "agents": [
  { "id": "claude", "name": "Claude Code", "description": "...",
    "adapter": "command", "timeoutMs": 900000,
    "cwd": "/path/to/project", "allowedRoots": ["/path/to/project"],
    "capabilities": { "newTasks": true, "nativeSessions": false, "streaming": false,
                      "cancellation": "process_signal" } }
] }
```

`timeoutMs` is the effective timeout for that agent. `cancellation` is `process_signal`
for command/stdio adapters and `request_only` for HTTP. `cwd`, `allowedRoots`, and
`delegateTo` (the agents its tasks may delegate to) appear only when configured.

### `POST /v1/tasks`

Body:

| Field | Required | Notes |
| --- | --- | --- |
| `agentId` | yes | Must match a registry id. |
| `input` | yes | Non-empty string; bounded by body and (for `command`) command-input limits. |
| `sessionId` | no | ≤128 chars; correlation tag. Defaults to a new UUID. |
| `requestId` | no | ≤128 chars; idempotency key scoped per agent. |
| `timeoutMs` | no | Positive integer. Overrides the agent/global timeout; rejected if above `A2A_RELAY_MAX_TIMEOUT_MS`. |
| `cwd` | no | Absolute or relative path. Must be inside the agent's `allowedRoots` (or equal to its `cwd`), else `400 cwd_not_allowed`. |
| `parentTaskId` | no | Task delegating this one (the MCP `delegate` tool sends `AGENT_RELAY_PARENT_TASK_ID`). Applies the delegation policy: `delegateTo`, cycles, `AGENT_RELAY_MAX_DELEGATION_DEPTH`, `AGENT_RELAY_MAX_DELEGATED_TASKS`. |

Unknown fields are rejected with `400 unknown_field`. Returns `202` with the task:

```json
{ "id": "uuid", "sessionId": "uuid", "agentId": "claude", "input": "...",
  "status": "queued", "createdAt": "ISO-8601", "timeoutMs": 900000,
  "cwd": "/path/to/project", "requestId": "..." }
```

Idempotent replay with the same `requestId` and identical fields returns `200` with the
original task. Different fields with the same `requestId` return `409`.

### `GET /v1/tasks/:id`

Returns the current task. Terminal tasks additionally include `startedAt`, `finishedAt`,
and usually `output` / `error` / `outputTruncated`. When `AGENT_RELAY_TASK_RETENTION_MS` is
set, a terminal task is dropped (and its `requestId` released) once that period has passed,
so its ID then returns `404 unknown_task`.

Add `?waitMs=<ms>` to long-poll: the server holds the request until the task is terminal or
the wait elapses (maximum `A2A_RELAY_MAX_WAIT_MS`). An invalid value returns `400 invalid_wait`.

### `GET /v1/tasks`

Lists stored tasks, most recent first. Query parameters:

| Parameter | Notes |
| --- | --- |
| `sessionId` | Filter by session. |
| `status` | One of the status values; anything else is `400 invalid_status`. |
| `limit` | Positive integer, capped at `A2A_RELAY_MAX_TASKS` (default 100). |

Returns `{ "tasks": [ ... ] }`.

### `DELETE /v1/tasks/:id`

Cancels a `queued` or `running` task and returns it with status `cancelled`. A cancelled
running task also carries `cancellation`: `process_signal` (the adapter's process group was
signalled) or `request_only` (HTTP: the request was not aborted). The task keeps its active
slot until the process exits or the request returns. Terminal tasks are returned unchanged.

### `POST /v1/admin/reload`

Reloads the registry from disk without dropping in-memory tasks. Requires the token and is
accepted only from loopback (`403 forbidden` otherwise). Returns `{ "ok": true, "agents": n, "registry": "..." }`.
An invalid registry returns `400 configuration_error` and keeps the running registry.

`SIGHUP` performs the same reload.

## Status values

`queued`, `running`, `completed`, `failed`, `timed_out`, `cancelled`.

## Error responses

| Status | `error` | Meaning |
| --- | --- | --- |
| 400 | `invalid_json`, `invalid_request`, `input_required`, `invalid_session_id`, `invalid_request_id`, `invalid_timeout`, `invalid_cwd`, `cwd_not_allowed`, `unknown_field`, `invalid_wait`, `invalid_status`, `invalid_limit`, `invalid_parent_task_id`, `unknown_parent_task`, `configuration_error` | Malformed request or registry. |
| 401 | `unauthorized` | Missing or wrong `Authorization: Bearer` token on a `/v1/` route. |
| 403 | `forbidden`, `origin_not_allowed`, `delegation_not_allowed` | Admin route called from a non-loopback address; request from a browser origin not in `AGENT_RELAY_ALLOWED_ORIGINS`; parent task's agent does not list the target in `delegateTo` (or is no longer registered). |
| 404 | `unknown_agent`, `unknown_task`, `not_found` | Missing agent/task/route. |
| 405 | `method_not_allowed` | Known route, wrong method. |
| 409 | `idempotency_conflict`, `delegation_cycle`, `delegation_depth_exceeded` | `requestId` reused with different fields; target already in the delegation chain; chain too deep. |
| 413 | `command_input_too_large` (and body-too-large) | Input/body exceeds a limit. |
| 415 | `unsupported_media_type` | `POST /v1/tasks` without `content-type: application/json`. |
| 429 | `delegation_budget_exhausted` | The root task's tree already created `AGENT_RELAY_MAX_DELEGATED_TASKS` tasks. |
| 500 | `request_failed` | Unexpected server error (includes `message`). |
| 503 | `task_capacity_reached` | Store full of non-terminal tasks. |

## MCP tools

The MCP server (JSON-RPC 2.0 over stdio, one JSON object per line) implements:

- `initialize` → `{ protocolVersion: "2025-06-18", capabilities, serverInfo }`
- `tools/list` → the six tools below
- `ping`
- `tools/call`

| Tool | Arguments | Result |
| --- | --- | --- |
| `list_agents` | `{}` | Same payload as `GET /v1/agents`. |
| `delegate` | `{ agentId, input, sessionId?, requestId?, timeoutMs?, cwd?, waitMs? }` | Submitted task; with `waitMs`, the terminal task. |
| `wait_task` | `{ taskId, maxWaitMs? }` | Terminal task, or the current task if the wait elapses. |
| `get_task` | `{ taskId }` | Current task. |
| `list_tasks` | `{ sessionId?, status?, limit? }` | `{ tasks: [...] }`. |
| `cancel_task` | `{ taskId }` | Cancelled task. |

Each result is returned as `content: [{ type: "text", text: "<json>" }]` plus
`structuredContent` holding the same object. Relay errors come back as `isError: true`
with `structuredContent` such as `{ "error": "unknown_task", "message": "...", "status": 404 }`.
Invalid tool arguments raise JSON-RPC error `-32602` before any HTTP call.

When the relay is unreachable and `A2A_RELAY_URL` is loopback, the MCP process starts
`src/server.py` detached (log `~/.local/state/agent-relay/relay.log`) and retries once.
Set `A2A_RELAY_AUTOSTART=0` to disable.

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
| `cwd` | if set | Effective working directory for the task. |
| `requestId` | if supplied | Echoed idempotency key. |
| `parentTaskId` | delegated tasks | Task that delegated this one. |
| `depth` | always | `0` for a root task, parent depth + 1 when delegated. |
| `startedAt` | running+ | ISO-8601. |
| `finishedAt` | terminal | ISO-8601. |
| `output` | success | Captured result text. |
| `error` | failure/timeout | Failure reason. |
| `outputTruncated` | on truncation | True when output hit the byte limit. |
| `cancellation` | cancelled while running | `process_signal` (adapter process group signalled) or `request_only` (HTTP request not aborted). |
