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
              "taskRetentionMs": null, "maxDelegationDepth": 2, "maxDelegatedTasks": 20 },
  "revision": { "commit": "131c0c0a3be70175e4bedaf4ef6074d0527f9cb9", "short": "131c0c0",
                "date": "2026-10-02T19:10:52Z" } }
```

`revision` is the git commit and commit date (ISO 8601) of the checkout the relay started
from, read once at startup. Each of its fields is `"unknown"` when the relay does not run
from a git checkout or git cannot answer. A relay older than this field omits `revision`.
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
    "options": { "effort": ["low", "high"] },
    "readOnly": "supported", "readOnlySandboxed": true,
    "capabilities": { "newTasks": true, "nativeSessions": false, "streaming": false,
                      "cancellation": "process_signal" } }
], "revision": { "commit": "131c0c0a3be70175e4bedaf4ef6074d0527f9cb9", "short": "131c0c0",
                 "date": "2026-10-02T19:10:52Z" } }
```

`revision` is the same object as in `GET /healthz`. `timeoutMs` is the effective timeout for that agent. `cancellation` is `process_signal`
for command/stdio adapters and `request_only` for HTTP. `cwd`, `allowedRoots`, and
`delegateTo` (the agents its tasks may delegate to) appear only when configured. `options`
maps each option name the agent declares to its allowed values, and appears only when the agent
declares options. `readOnly` is `supported` or `unsupported` for every agent: `supported` means
the agent has a `readOnly` profile that works on this host, and `readOnlySandboxed` (present only
then) says whether the relay also runs it under a sandbox that blocks writes under the task's
`cwd`, the agent's `allowedRoots` and the git metadata of the `cwd` (a worktree's too), renaming or
deleting any directory above those trees, reading the relay token, and connecting to the relay port.
The sandbox does not block reads, other network access, or writes made for the agent by other
local services; see the README section "What the sandbox blocks, and what it does not".

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
| `waitMs` | no | The wait you mean to use next on `GET /v1/tasks/:id?waitMs=`. The relay checks it before it creates the task: a value that is not an integer from 1 to `A2A_RELAY_MAX_WAIT_MS` returns `400 invalid_wait` (message names the range) and creates nothing. The submission does not wait; it still returns `202`. It is not stored on the task. |
| `options` | no | Object of option name to value string, for example `{"effort": "high"}`. Each name and value must be declared in the agent's registry `options`; the relay adds the args the owner mapped to them after the agent's `args` and before `input`. An undeclared name is `400 unknown_option`, an undeclared value `400 invalid_option_value`, a non-object or non-string value `400 invalid_options`. |
| `readOnly` | no | `true` requires a run that cannot change files: the agent's `readOnly` profile args are added after its `args` and options and, when the profile sets `sandbox`, the run is wrapped in `sandbox-exec`. An agent without a profile (or whose sandbox needs macOS) returns `400 read_only_unsupported`; a sandboxed profile with no `cwd` on the task or agent returns `400 cwd_required`; a value that is not `true` or `false`, including `null`, returns `400 invalid_read_only`. For a `command` agent whose profile has `args` and no `inputSeparator`, an `input` that starts with `-` returns `400 invalid_input` (it could be read as an option that overrides the profile's args); with an `inputSeparator` the relay puts it before the input and any input is accepted. A `stdio` agent is not checked. The relay checks the task against the registry entry again when it dispatches a queued task, so a reload in between cannot turn a read-only task into an unenforced one: the task ends `failed` and its `error` starts with the code submission would have given (see "Task errors" below). Default `false`. |
| `parentTaskId` | no | Task delegating this one (the MCP `delegate` tool sends `AGENT_RELAY_PARENT_TASK_ID`). Applies the delegation policy: `delegateTo`, cycles, `AGENT_RELAY_MAX_DELEGATION_DEPTH`, `AGENT_RELAY_MAX_DELEGATED_TASKS`. |

Unknown fields are rejected with `400 unknown_field`. Returns `202` with the task:

```json
{ "id": "uuid", "sessionId": "uuid", "agentId": "claude", "inputChars": 42,
  "status": "queued", "createdAt": "ISO-8601", "timeoutMs": 900000,
  "cwd": "/path/to/project", "requestId": "..." }
```

Task objects never echo `input`: they carry `inputChars` (its length in characters). Only
`GET /v1/tasks/:id?includeInput=true` returns it.

Idempotent replay with the same `requestId` and identical fields returns `200` with the
original task. Different fields with the same `requestId` (including different `options` or `readOnly`) return `409`.

### `GET /v1/tasks/:id`

Returns the current task. Terminal tasks additionally include `startedAt`, `finishedAt`,
and usually `output` / `error` / `outputTruncated`. When `AGENT_RELAY_TASK_RETENTION_MS` is
set, a terminal task is dropped (and its `requestId` released) once that period has passed,
so its ID then returns `404 unknown_task`.

Add `?waitMs=<ms>` to long-poll: the server holds the request until the task is terminal or
the wait elapses (maximum `A2A_RELAY_MAX_WAIT_MS`). A value that is not an integer from 1 to
that maximum returns `400 invalid_wait`, and its `message` names the range. When the wait
elapses and the task is still `queued` or `running`, the `200` response also carries
`waitExpired: true`, `waitedMs` (the wait that elapsed), and a `hint`: call again with the
same task ID. A terminal task has none of these fields, and they are never stored on the task.

Add `?includeInput=true` to get the submitted prompt back as `input`; the default is `false`.
Any other value returns `400 invalid_include_input`. The task always carries `inputChars`.

### `GET /v1/tasks`

Lists stored tasks, most recent first. Query parameters:

| Parameter | Notes |
| --- | --- |
| `sessionId` | Filter by session. |
| `status` | One of the status values; anything else is `400 invalid_status`. |
| `limit` | Positive integer, capped at `A2A_RELAY_MAX_TASKS` (default 100). |

Returns `{ "tasks": [ ... ] }`. Each task carries `inputChars` and never `input`; list takes no `includeInput`.

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
| 400 | `invalid_json`, `invalid_request`, `input_required`, `invalid_session_id`, `invalid_request_id`, `invalid_timeout`, `invalid_cwd`, `cwd_not_allowed`, `unknown_field`, `invalid_options`, `unknown_option`, `invalid_option_value`, `invalid_read_only`, `read_only_unsupported`, `cwd_required`, `invalid_input`, `invalid_wait`, `invalid_include_input`, `invalid_status`, `invalid_limit`, `invalid_parent_task_id`, `unknown_parent_task`, `configuration_error` | Malformed request or registry. |
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

### Task errors

A task can also end `failed` after it was accepted, with a code at the start of `error`
(`<code>: <message>`):

| Code | Meaning |
| --- | --- |
| `read_only_unsupported`, `cwd_required`, `invalid_input`, `unknown_option`, `invalid_option_value` | A registry reload while the task was queued changed its agent, and the current entry no longer allows what the task asked for. The same codes as at submission; nothing ran. |
| `read_only_git_unknown` | A sandboxed `readOnly` task's `cwd` is in a git repository (a `.git` file or directory, or a directory with `HEAD`, `objects` and `refs` as a bare repository has, exists at or above it) but `git rev-parse` could not say where its git metadata is: git missing or not runnable, slower than 5 s or than the task's remaining time, non-zero exit (including a repository whose owner git does not trust: the relay ignores the global git config, so `safe.directory` there does not help), or unusable output. The relay runs its trusted git (`/usr/bin/git`, or the owner's `AGENT_RELAY_GIT`) by absolute path in a minimal environment. It fails closed and does not run the task with less protection. This is a failed task and not a `400`, because the lookup runs when the sandbox is built, at dispatch. A `cwd` with no such sign above it needs no git and runs. |

A task whose time runs out while the relay is still locating git metadata ends `timed_out` and its adapter never starts.

## MCP tools

The MCP server (JSON-RPC 2.0 over stdio, one JSON object per line) implements:

- `initialize` → `{ protocolVersion: "2025-06-18", capabilities, serverInfo }`
- `tools/list` → the six tools below
- `ping`
- `tools/call`

| Tool | Arguments | Result |
| --- | --- | --- |
| `list_agents` | `{}` | Same payload as `GET /v1/agents`, including `revision`. |
| `delegate` | `{ agentId, input, sessionId?, requestId?, timeoutMs?, cwd?, options?, readOnly?, waitMs? }` | Submitted task; with `waitMs`, the terminal task, or the current task with `waitExpired: true` if the wait elapses first. A `waitMs` above the relay limit is refused with `invalid_wait` before the task is created, so nothing runs without an ID. The tool then retries once with the limit (read from `/healthz` `limits.maxWaitMs`, never longer than asked) and adds `waitMs` and `waitClamped: true` to the result. Even with a `requestId` the retry makes one task, because the refused attempt created none. |
| `wait_task` | `{ taskId, maxWaitMs? }` | Terminal task, or the current task with `waitExpired: true`, `waitedMs` and `hint` if the wait elapses. A `maxWaitMs` above the relay limit is clamped to it, and the result adds `waitMs` and `waitClamped: true`. |
| `get_task` | `{ taskId, includeInput? }` | Current task. With `includeInput: true` it also carries `input`. |
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
| `inputChars` | always | Length of the submitted prompt in characters. |
| `input` | only `GET /v1/tasks/:id?includeInput=true` | The submitted prompt. Left out of every other task object. |
| `status` | always | See status values. |
| `createdAt` | always | ISO-8601. |
| `timeoutMs` | always | Effective timeout. |
| `cwd` | if set | Effective working directory for the task. |
| `options` | if supplied | The option values the task chose, for example `{"effort": "high"}`. |
| `readOnly` | if `true` | The task was submitted as read-only. |
| `requestId` | if supplied | Echoed idempotency key. |
| `parentTaskId` | delegated tasks | Task that delegated this one. |
| `depth` | always | `0` for a root task, parent depth + 1 when delegated. |
| `startedAt` | running+ | ISO-8601. |
| `finishedAt` | terminal | ISO-8601. |
| `output` | success | Captured result text. |
| `error` | failure/timeout | Failure reason. |
| `outputTruncated` | on truncation | True when output hit the byte limit. |
| `cancellation` | cancelled while running | `process_signal` (adapter process group signalled) or `request_only` (HTTP request not aborted). |
