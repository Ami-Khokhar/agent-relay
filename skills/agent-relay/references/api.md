# agent-relay API reference

The HTTP service is the source of truth. MCP tools are a thin proxy over it.

Every `/v1/` route requires `Authorization: Bearer <token>`. The token is
`AGENT_RELAY_TOKEN`, else the file `~/.config/agent-relay/token` (override with
`AGENT_RELAY_TOKEN_FILE`), which the relay creates with mode `0600` on first start. A request
without the token gets `401` whose message names that file. Requests
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
              "maxSteps": 64, "maxWorkflows": 200, "maxForEach": 32,
              "taskRetentionMs": null, "maxDelegationDepth": 2, "maxDelegatedTasks": 20 },
  "revision": { "commit": "131c0c0a3be70175e4bedaf4ef6074d0527f9cb9", "short": "131c0c0",
                "date": "2026-10-02T19:10:52Z" },
  "update": { "at": "2026-11-08T07:11:04Z", "outcome": "restarted",
              "from": "324c1cda792adca3b824feb382af87b08c397727",
              "to": "3e16ee197e337baa2b73e28ffa3880a2374f3715",
              "pendingRestart": false } }
```

`revision` is the git commit and commit date (ISO 8601) of the checkout the relay started
from, read once at startup. Each of its fields is `"unknown"` when the relay does not run
from a git checkout or git cannot answer. A relay older than this field omits `revision`.
`update` is the last `scripts/update.sh` attempt recorded for this checkout, read on every
request: `outcome` is `up_to_date`, `updated`, `restarted` or `failed`, and `pendingRestart`
says a restart is still recorded as pending. It is `null` when no attempt is recorded for
this checkout (the script has never run here, or its record could not be read), and a relay
older than this field omits `update`. Only a plain regular file is read: a symlink, a FIFO,
a directory or an oversized record at that path reports as no attempt instead of blocking
the health check.
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
    "model": "glm-5.3-flash", "tags": ["reviewer"],
    "adapter": "command", "timeoutMs": 900000,
    "cwd": "/path/to/project", "allowedRoots": ["/path/to/project"],
    "options": { "effort": ["low", "high"] },
    "readOnly": "supported", "readOnlySandboxed": true,
    "role": "reviewer", "roleDescription": "Reviews a change against the stated intent.",
    "capabilities": { "newTasks": true, "nativeSessions": false, "streaming": false,
                      "cancellation": "process_signal" } }
], "revision": { "commit": "131c0c0a3be70175e4bedaf4ef6074d0527f9cb9", "short": "131c0c0",
                 "date": "2026-10-02T19:10:52Z" } }
```

`revision` is the same object as in `GET /healthz`. `timeoutMs` is the effective timeout for that agent. `cancellation` is `process_signal`
for command/stdio adapters and `request_only` for HTTP. `role` and `roleDescription` appear only
when the agent names a role in the registry; the instructions themselves are never listed. `model`
and `tags` appear only when the registry declares them: `model` is the model that worker runs and
`tags` are its labels, so a caller selects by data rather than by matching `name` prose. Both must be
non-blank strings, and both are stored trimmed, so the listing shows the value that was validated (which
is why `?tag=%20reviewer` matches nothing).
`GET /v1/agents?tag=<name>` keeps only the agents carrying that tag — an agent must carry every
tag the query names, and an unknown tag returns an empty list rather than an error. `cwd`, `allowedRoots`, and
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
| `input` | yes | Non-empty string; bounded by body and (for `command`) command-input limits. A JSON escape such as `"\ud800"` decodes to a lone surrogate, which is not UTF-8 text and returns `400 invalid_input`. |
| `sessionId` | no | ≤128 chars; correlation tag. Defaults to a new UUID. |
| `requestId` | no | ≤128 chars; idempotency key scoped per agent. |
| `timeoutMs` | no | Positive integer. Overrides the agent/global timeout; rejected if above `A2A_RELAY_MAX_TIMEOUT_MS`. |
| `cwd` | no | Absolute or relative path. Must be inside the agent's `allowedRoots` (or equal to its `cwd`), else `400 cwd_not_allowed`; a path carrying a NUL or a lone surrogate returns `400 invalid_cwd`. |
| `waitMs` | no | Long-poll the submission itself, the way `GET /v1/tasks/:id?waitMs=` does. The relay checks it before it creates the task: a value that is not an integer from 1 to `A2A_RELAY_MAX_WAIT_MS` returns `400 invalid_wait` (message names the range and points at `GET /v1/tasks/:id?waitMs=`) and creates nothing. A valid wait then blocks: the call returns `200` with the task once it is terminal, or the current task with `waitExpired: true`, `waitedMs` and a `hint` when the wait elapses first. If the store drops the task before the waiter reads it — retention expired, or a new submission needed the room — the call answers `404 unknown_task` instead of a task copy, exactly as `GET /v1/tasks/:id` does. A submission without `waitMs` still returns `202` at once. It is not stored on the task. The check sits after the idempotent-replay decision: a replay (`requestId` matching a stored task with identical fields) ignores the wait and returns `200` with the stored task without waiting, because the wait is advisory and a replay creates nothing; a replay with changed fields still returns `409`. |
| `options` | no | Object of option name to value string, for example `{"effort": "high"}`. Each name and value must be declared in the agent's registry `options`; the relay adds the args the owner mapped to them after the agent's `args` and before `input`. An undeclared name is `400 unknown_option`, an undeclared value `400 invalid_option_value`, a non-object or non-string value `400 invalid_options`. |
| `readOnly` | no | `true` requires a run that cannot change files: the agent's `readOnly` profile args are added after its `args` and options and, when the profile sets `sandbox`, the run is wrapped in `sandbox-exec`. An agent without a profile (or whose sandbox needs macOS) returns `400 read_only_unsupported`; a sandboxed profile with no `cwd` on the task or agent returns `400 cwd_required`; a value that is not `true` or `false`, including `null`, returns `400 invalid_read_only`. For a `command` agent whose profile has `args` and no `inputSeparator`, an `input` that starts with `-` returns `400 invalid_input` (it could be read as an option that overrides the profile's args); the check reads the text the command receives, so a role that prepends its instructions makes such an input safe; with an `inputSeparator` the relay puts it before the input and any input is accepted. A `stdio` agent is not checked. The relay checks the task against the registry entry again when it dispatches a queued task, so a reload in between cannot turn a read-only task into an unenforced one: the task ends `failed` and its `error` starts with the code submission would have given (see "Task errors" below). Default `false`. |
| `parentTaskId` | no | Task delegating this one (the MCP `delegate` tool sends `AGENT_RELAY_PARENT_TASK_ID`). Applies the delegation policy: `delegateTo`, cycles, `AGENT_RELAY_MAX_DELEGATION_DEPTH`, `AGENT_RELAY_MAX_DELEGATED_TASKS`. |

Unknown fields are rejected with `400 unknown_field`. Returns `202` with the task:

```json
{ "id": "uuid", "sessionId": "uuid", "agentId": "claude", "inputChars": 42,
  "status": "queued", "createdAt": "ISO-8601", "timeoutMs": 900000,
  "cwd": "/path/to/project", "requestId": "...", "role": "reviewer",
  "rolePromptSha256": "24ec5ae71354" }
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

### `POST /v1/workflows`

The body **is** the workflow document, not a wrapper, and `content-type: application/json`
is required (`415 unsupported_media_type` otherwise).

| Field | Required | Notes |
| --- | --- | --- |
| `steps` | yes | 1 to `A2A_RELAY_MAX_STEPS` (default 64) step objects. |
| `name` | no | ≤128 chars; defaults to `workflow`. |
| `sessionId` | no | ≤128 chars. Defaults to the run ID and is passed to every child task. |
| `requestId` | no | ≤128 chars; idempotency key for the submission itself. |

Step fields:

| Field | Required | Notes |
| --- | --- | --- |
| `id` | yes | Unique in the document; `[A-Za-z0-9_.~-]`, ≤128 chars. |
| `agentId` | yes | Must match a registry id. |
| `input` | yes | Non-empty prompt. May contain `{{stepId.output}}`, and `{{item}}` / `{{index}}` on a `foreach` step. A JSON escape such as `"\ud800"` decodes to a lone surrogate, which is not UTF-8 text and returns `400 invalid_input` naming the step's index and field. |
| `needs` | no | Step ids that must complete first. Acyclic; forward references allowed. |
| `needsPolicy` | no | `"all"` (default) or `"any"`. `any` requires at least one `needs` entry, otherwise `400 invalid_needs_policy`. With `all`, every need must complete. With `any`, every need must be terminal and at least one completed; without a completed need the step is skipped with `dependency_not_completed`. In an `any` prompt, `{{id.output}}` of a failed (including timed-out), skipped or cancelled ancestor becomes a marker like `[id did not complete: status=failed error=…]`. Unfinished ancestors stay empty under either policy; `all` also keeps non-completed terminal ancestors empty. A tolerated failed/skipped branch still makes the run `failed`; run cancellation remains `cancelled`. |
| `foreach` | no | 1 to `A2A_RELAY_MAX_FOREACH` (default 32) strings, each ≤1024 chars. |
| `retries` | no | `0` to `5`; extra attempts after a task fails or times out. |
| `repeat` | no | `{ "max": 1..10, "untilOutputMatches": "<regex>" }`. Not allowed with `foreach`. |
| `failIfOutputMatches` | no | Python regular expression. A match on the output of a completed task fails the step with `output_matched_fail_pattern` and skips its dependents. The pattern is tested against each task's own output, so on a `foreach` step every item is matched separately and never the JSON array the step aggregates: one item reading `^BLOCKED` fails the whole step, while `^\[` does not fire on the serialization alone. |
| `timeoutMs` | no | Positive integer, same cap as a task. |
| `cwd` | no | Same `allowedRoots` check as a task. A path carrying a NUL or a lone surrogate returns `400 invalid_cwd` naming the step's index and field. |
| `options` | no | `{"<name>": "<value>"}`, exactly as for a task submission. The agent decides which names and values it allows. |
| `readOnly` | no | Boolean, exactly as for a task submission. |

References are resolved at submit time. `{{stepId.output}}` (or bare `{{stepId}}`) must
name a step in that step's transitive `needs`, so a run cannot read a sibling that has not
run yet. `{{item}}` / `{{index}}` and `{{foreach.item}}` / `{{foreach.index}}` require
`foreach` on that step. Anything else is `400 unknown_reference`; an unterminated
placeholder is `400 invalid_template`.

Returns `202` with the run. Replaying the same `requestId` with the same document returns
`200`; with a different document, `409 idempotency_conflict`.

Validation failures use `400` with: `invalid_steps`, `invalid_step_id`, `duplicate_step`,
`unknown_step`, `cyclic_dependency`, `invalid_needs`, `invalid_needs_policy`, `invalid_foreach`, `invalid_retries`,
`invalid_repeat`, `invalid_fail_if`, `invalid_template`, `unknown_reference`, `invalid_options`,
`invalid_read_only`, `command_input_too_large`, and the shared `unknown_field`,
`unknown_agent`, `input_required`, `invalid_input`, `invalid_name`, `invalid_session_id`,
`invalid_request_id`, `invalid_timeout`, `invalid_cwd`, `cwd_not_allowed`.

A step whose `options` or `readOnly` the agent cannot honour fails when the relay launches
it, with the relay's own code at the start of `steps[].error` (`unknown_option: ...`,
`invalid_option_value: ...`, `read_only_unsupported: ...`), and no task for it runs. The
shape of those two fields is checked at submit time; what the agent allows is checked when
the step starts, exactly as a registry change would be for a queued task.

### `GET /v1/workflows/:id`

Returns the run: `id`, `name`, `status`, `sessionId`, `createdAt`, `startedAt`,
`finishedAt`, `error`, `spec`, and `steps[]` with `id`, `agentId`, `needs`, `needsPolicy`, `status`,
`attempts`, `startedAt`, `finishedAt`, `taskIds`, `attemptTimes`, `output`, `error`, and
(for a failed step with a failing task) `errorDetail`.
Every step carries `startedAt` and `finishedAt`: `startedAt` is stamped when the relay
begins the attempt (rendering and submitting its tasks), `finishedAt` when the step becomes
terminal. A step that never started — `pending`, `skipped` with
`dependency_not_completed`, or `cancelled` with the run before its attempt began — keeps
both null. A step whose attempt failed before submitting any task (an unresolved template
reference, or a submission the relay refuses, such as `read_only_unsupported: ...`) is
`failed` with both stamped and `attempts` `0`. `attemptTimes` is one
`{startedAt, finishedAt}` window per submitted attempt, oldest first, so a `retries` or
`repeat` step reports each try separately; it is empty whenever no attempt submitted a
task.

`?view=summary` returns a small projection for an orchestrator that only needs to know
which step finished: no `spec`, and each step carries its identity, `needs`, `status`,
`attempts`, time windows, `error`, plus `outputChars` (the output's length) and `firstLine`
(its first line, capped at 200 characters including a trailing `…` when truncated)
instead of the full `output`, `taskIds`, and `errorDetail`. The summary is smaller than
the full view whenever an output is long.

`?step=<stepId>` returns that one step alone, with its full `output`, `taskIds`,
`attemptTimes`, and (for a failed step with a failing task) `errorDetail`, and no `spec`. A step the run does
not have is `404 unknown_step`, returned before long-polling; a `step` that is not a
valid step id shape is `400 invalid_step`. Both selectors combine with `?waitMs=`.
With both selectors, `view=summary` determines the selected step's shape: `outputChars`
and bounded `firstLine` replace full `output`, `taskIds` and `errorDetail`.

`?waitMs=<ms>` long-polls until the run is terminal or the wait elapses; a wait that ends
first adds `waitExpired: true`, `waitedMs` and `hint`, exactly as a task wait does. A
`waitMs` outside `1..maxWaitMs` returns `400 invalid_wait`. A `view` other than `summary`
returns `400 invalid_view`.

A run that has left memory but still has a journal returns `404 unknown_workflow` with a
`message` pointing at `POST /v1/workflows/:id/resume`.

### `GET /v1/workflows`

Same query parameters as `GET /v1/tasks` (`sessionId`, `status`, `limit`), restricted to
workflow statuses. Entries omit `spec`, and steps omit `output`, `taskIds`,
`attemptTimes`, and `errorDetail`. Returns `{ "workflows": [ ... ] }`, most recent first.

### `GET /v1/workflows/events`

One ordered change feed per session. `sessionId` is required; `after` (default `0`) is the
seq of the last event the caller saw; `waitMs` long-polls for the next change and ends with
an empty list when it elapses. Returns `{ "events": [ ... ] }`, oldest first, each event:

```json
{ "seq": 17, "workflowId": "wf-...", "stepId": "review",
  "oldStatus": "running", "newStatus": "completed" }
```

`seq` rises across the relay; pass the last one seen as `after` to continue. `stepId` is
`null` for a run-level status change (the run's own `queued` → `running` → terminal
movement); otherwise it is a step of that run changing between its public statuses
(`pending`, `running`, `completed`, `failed`, `skipped`, `cancelled`). A `waitMs` outside
`1..maxWaitMs` returns `400 invalid_wait`, exactly as on `GET /v1/workflows/:id`; an
`after` that is not a non-negative integer returns `400 invalid_after`. The feed lives in
memory and keeps at most 4096 events in total: the oldest fall off, and an `after` older
than that still works, simply skipping what is gone.

### `DELETE /v1/workflows/:id`

Cancels the run, cancels every task it started, and returns the run as `cancelled`.
An already terminal run is returned unchanged.

### `POST /v1/workflows/:id/resume`

Reads `<state dir>/workflows/<id>.jsonl`, restores the steps that completed with their
outputs, attempt counts, and time windows, and schedules the rest. Returns `202` with the
restarted run. `409 workflow_active` when the run is still going, `404 unknown_workflow`
when nothing is journaled under that id, and `503 workflow_capacity_reached` when the store
is already at `maxWorkflows` with no finished run to evict and the id is not itself stored:
a finished run that is still stored is resumed in place, while a journal-only id takes a
slot by the same eviction rule as a submission. The request takes no body and needs no
`content-type`.

A resume continues the change feed rather than replaying it: a step restored from the
journal publishes no event, so an `after` cursor taken before the resume does not re-report
its completion. A step the resume will run keeps the `pending` baseline, so its attempt
publishes whatever it does — including failing before it ever reaches `running`.

A run with nothing left to run (every step was restored) finishes at the status its feed
already reported, so it publishes no run-level event while its previous run is still in
relay memory; a run with work left, and one whose previous object was evicted by the
workflow store bound, publishes its own `queued → running → terminal`.

### `GET /v1/pressure`

The host reading, with a verdict a caller can act on:

```json
{ "verdict": "ok", "reasons": [],
  "admitBelow": "critical",
  "memory": { "pressureLevel": "normal", "freePercent": 47 },
  "swap": { "usedBytes": 955451965, "totalBytes": 2147483648, "usedPercent": 44 },
  "thermal": { "speedLimitPercent": null },
  "temperatureC": null,
  "temperatureReason": "macOS exposes no unprivileged CPU temperature; powermetrics needs root",
  "sampledAt": "2026-10-25T11:23:38.461Z",
  "guard": { "enabled": true, "stopsAt": "critical", "holdMs": 30000 } }
```

`verdict` is `critical` when the kernel reports critical memory pressure (`vm.memory_pressure`),
less than 4% of memory is free (`kern.memorystatus_level`), swap is at least 90% used
(`vm.swapusage`), or the CPU is throttled below 40% of its speed (`pmset -g therm`); `warn` uses
the softer readings (kernel warning, 12% free, 70% swap, 75% throttled); otherwise `ok`.
`reasons` names the reading that decided it. Every signal is `null` when the host cannot report
it, so a missing probe never fails the call.

`admitBelow` is the owner's admission line: the verdict at or above which the relay refuses a new
workflow, read from the first nonempty `AGENT_RELAY_ADMIT_BELOW` or `A2A_RELAY_ADMIT_BELOW`
(default `critical`). Invalid values stop startup and name the selected variable. The refusal
and this field come from the same reading, so they cannot disagree.

`guard` says whether the relay stops active runs on its own, at which verdict, and after how long:
`holdMs` is `A2A_RELAY_PRESSURE_HOLD_MS` (default 30000), the time the verdict must stay
`critical` before a running workflow is stopped, read at least twice so one spike between normal
readings costs a run nothing. `holdMs` 0 stops on the first critical reading. A run the guard
stops is `cancelled` with `error` beginning `device_pressure:`, naming how long pressure stayed
critical and the readings that decided it.

`POST /v1/workflows` returns `503 device_pressure` with this body while the verdict is at or
above `admitBelow` (`AGENT_RELAY_ADMIT_BELOW`, default `critical`), and the message names the
reading that decided it (or the setting itself when an `ok` line refuses a calm host). The
remedy asks the owner to address those readings and retry; it is not limited to memory.
`temperatureC` is null unless the owner allowlists an unprivileged reader in
`AGENT_RELAY_TEMP_READER` (for example `macmon`); `AGENT_RELAY_TEMP_WARN_C` adds a `warn` reason
at or above that many degrees. The threshold must be finite; invalid or non-finite values
stop startup with a setting-specific error. A configured reader with invalid quoting or a
non-absolute/non-executable path leaves temperature null with a configuration reason rather
than the missing-sensor reason. `thermal.speedLimitPercent` is null when macOS has recorded
no throttle.

### `POST /v1/pressure/kill`

Stops every active run now, whether or not the verdict is critical, and keeps each journal so
`resume` can continue it. Takes no body. Returns
`{ "killed": ["wf-..."], "reason": "device_pressure: ...", "pressure": { ... } }`. A stopped
run is `cancelled` with `device_pressure:` at the start of its `error`.

### `POST /v1/admin/reload`

Reloads the registry from disk without dropping in-memory tasks. Requires the token and is
accepted only from loopback (`403 forbidden` otherwise). Returns `{ "ok": true, "agents": n, "registry": "..." }`.
An invalid registry returns `400 configuration_error` and keeps the running registry.

`SIGHUP` performs the same reload.

## Status values

Tasks: `queued`, `running`, `completed`, `failed`, `timed_out`, `cancelled`.

Workflows: `queued`, `running`, `completed`, `failed`, `cancelled`. Workflow steps:
`pending`, `running`, `completed`, `failed`, `skipped`, `cancelled`.

## Error responses

| Status | `error` | Meaning |
| --- | --- | --- |
| 400 | `invalid_json`, `invalid_request`, `input_required`, `invalid_session_id`, `invalid_request_id`, `invalid_timeout`, `invalid_cwd`, `cwd_not_allowed`, `unknown_field`, `invalid_options`, `unknown_option`, `invalid_option_value`, `invalid_read_only`, `read_only_unsupported`, `cwd_required`, `invalid_input`, `invalid_wait`, `invalid_after`, `invalid_include_input`, `invalid_status`, `invalid_limit`, `invalid_parent_task_id`, `unknown_parent_task`, `configuration_error`, and the workflow validation codes listed under `POST /v1/workflows` | Malformed request or registry. |
| 401 | `unauthorized` | Missing or wrong `Authorization: Bearer` token on a `/v1/` route; the message names the token file. |
| 403 | `forbidden`, `origin_not_allowed`, `delegation_not_allowed` | Admin route called from a non-loopback address; request from a browser origin not in `AGENT_RELAY_ALLOWED_ORIGINS`; parent task's agent does not list the target in `delegateTo` (or is no longer registered). |
| 404 | `unknown_agent`, `unknown_task`, `unknown_workflow`, `not_found` | Missing agent/task/run/route. |
| 405 | `method_not_allowed` | Known route, wrong method. |
| 409 | `idempotency_conflict`, `workflow_active`, `delegation_cycle`, `delegation_depth_exceeded` | `requestId` reused with different fields; resume requested while the run is still going; target already in the delegation chain; chain too deep. |
| 413 | `command_input_too_large`, `role_input_too_large` (and body-too-large) | Input plus a role's instructions, or the body, exceeds a limit. |
| 415 | `unsupported_media_type` | `POST /v1/tasks` or `POST /v1/workflows` without `content-type: application/json`. |
| 429 | `delegation_budget_exhausted` | The root task's tree already created `AGENT_RELAY_MAX_DELEGATED_TASKS` tasks. |
| 500 | `request_failed` | Unexpected server error (includes `message`). |
| 503 | `task_capacity_reached`, `workflow_capacity_reached`, `device_pressure` | Store full of non-terminal tasks or of runs with none finished (submissions and resumes alike); or a workflow submission while the host verdict is at or above the relay's admitBelow line, with the reading attached. |

### Task errors

A task can also end `failed` after it was accepted, with a code at the start of `error`
(`<code>: <message>`):

| Code | Meaning |
| --- | --- |
| `read_only_unsupported`, `cwd_required`, `invalid_input`, `unknown_option`, `invalid_option_value` | A registry reload while the task was queued changed its agent, and the current entry no longer allows what the task asked for. The same codes as at submission; nothing ran. |
| `role_removed`, `role_input_too_large` | A role worker's task was queued and the reloaded entry either lost its `role` or carries instructions that no longer fit the command line. Nothing ran, and the record keeps the role it was accepted under. |
| `read_only_git_unknown` | A sandboxed `readOnly` task's `cwd` is in a git repository (a `.git` file or directory, or a directory with `HEAD`, `objects` and `refs` as a bare repository has, exists at or above it) but `git rev-parse` could not say where its git metadata is: git missing or not runnable, slower than 5 s or than the task's remaining time, non-zero exit (including a repository whose owner git does not trust: the relay ignores the global git config, so `safe.directory` there does not help), or unusable output. The relay runs its trusted git (`/usr/bin/git` or `/bin/git` on macOS and Linux, machine-wide `%ProgramFiles%\Git\cmd\git.exe` on Windows — a per-user or `PATH`-found git is refused, since a task could plant it — or the owner's `AGENT_RELAY_GIT`) by absolute path in a minimal environment. It fails closed and does not run the task with less protection. This is a failed task and not a `400`, because the lookup runs when the sandbox is built, at dispatch. A `cwd` with no such sign above it needs no git and runs. |

A task whose time runs out while the relay is still locating git metadata ends `timed_out` and its adapter never starts.

## MCP tools

The MCP server (JSON-RPC 2.0 over stdio, one JSON object per line) implements:

- `initialize` → `{ protocolVersion: "2025-06-18", capabilities, serverInfo, instructions }`.
  `instructions` carries the usage guidance, an optional cached release notice, and — when the
  install changed on disk after this process started — a notice that says so in words, naming
  the checkout's current commit and the commit this server loaded, and asking the client to
  restart the MCP server (a new session or an MCP reconnect). The notice is raised only for a
  source file this process actually imported, so a change elsewhere in the install never raises
  it, and the server never restarts itself: the client owns that process, and the relay picks
  up its own code on its next restart. Without such a change `instructions` has no notice.
- `tools/list` → the fourteen tools below
- `ping`
- `tools/call`

| Tool | Arguments | Result |
| --- | --- | --- |
| `check_updates` | `{ includePrereleases? }` | Installed version and either no offer or an available version, canonical release URL, and bounded release notes; `releaseNotesTruncated` is `true` when the release body was cut at the bound, so the remainder is available only at that URL. Calls the canonical GitHub release API only when explicitly requested; notes are data, not instructions. It never installs or restarts. |
| `list_agents` | `{}` | Same payload as `GET /v1/agents`, including `revision`. |
| `delegate` | `{ agentId, input, sessionId?, requestId?, timeoutMs?, cwd?, options?, readOnly?, waitMs? }` | The queued task as soon as the relay accepts it — no wait is asked for unless `waitMs` is given. With `waitMs`, the terminal task, or the current task with `waitExpired: true` if the wait elapses first. A `waitMs` above the relay limit is refused with `invalid_wait` before the task is created, so nothing runs without an ID. The tool then retries once with the limit (read from `/healthz` `limits.maxWaitMs`, never longer than asked) and adds `waitMs` and `waitClamped: true` to the result. Even with a `requestId` the retry makes one task, because the refused attempt created none. A refusal that lands on the follow-up long poll instead — for example a replay that returns `200` without re-checking the wait — is clamped to the limit the same way, and the result then carries `waitMs` and `waitClamped: true`. If the submission created the task but the follow-up long poll then fails with no clamp available — a relay that refuses that wait while its `/healthz` reports no usable limit, an unreachable `/healthz`, or a transport error — the result is the created task with `waitFailed: true` and a `waitError` naming the failure and pointing at `wait_task` with the same `taskId`; the tool never resubmits after a successful submission. |
| `wait_task` | `{ taskId, maxWaitMs? }` | The task's current state, at once when `maxWaitMs` is omitted. With `maxWaitMs`, the terminal task, or the current task with `waitExpired: true`, `waitedMs` and `hint` if the wait elapses. A `maxWaitMs` above the relay limit is clamped to it, and the result adds `waitMs` and `waitClamped: true`. |
| `get_task` | `{ taskId, includeInput? }` | Current task. With `includeInput: true` it also carries `input`. |
| `list_tasks` | `{ sessionId?, status?, limit? }` | `{ tasks: [...] }`. |
| `cancel_task` | `{ taskId }` | Cancelled task. |
| `run_workflow` | `{ spec \| specPath, waitMs? }` | Submitted run; with `waitMs`, the terminal run, or the current run with `waitExpired: true` if the wait elapses first. `spec` is the workflow document itself, typed with the full step schema in `tools/list`. Pass `specPath` instead for the absolute path of a JSON file holding that document: the MCP server reads the file and submits its content unchanged, exactly as if it had been passed as `spec`. Exactly one of `spec` and `specPath` is required; both, neither, a missing file, a relative path, invalid JSON or a file that is not UTF-8 text is refused with JSON-RPC `-32602` and no run is submitted. A wait above the relay's `A2A_RELAY_MAX_WAIT_MS` is clamped to that limit, never raised, and the result then carries `waitMs` and `waitClamped: true`. |
| `get_workflow` | `{ workflowId, view?, step?, maxWaitMs? }` | The run with every step and its output; with `maxWaitMs`, the terminal run, or the current run with `waitExpired: true`. A wait above the relay's `A2A_RELAY_MAX_WAIT_MS` is clamped to that limit, never raised, and the result then carries `waitMs` and `waitClamped: true`. `view: "summary"` omits the spec and each step's full output, carrying `outputChars` and `firstLine` instead; `step` returns that one step with its full output and `errorDetail`. Both ride along on the long-poll path. |
| `wait_workflow_events` | `{ sessionId, after?, maxWaitMs? }` | The session's change feed: `{ events: [ { seq, workflowId, stepId, oldStatus, newStatus } ] }`, oldest first. With `maxWaitMs` it waits up to that long for the next change and returns an empty list when the wait ends; without it, it returns the changes recorded so far at once and waits for nothing. `after` (default 0) is the seq of the last event seen. A `maxWaitMs` above the relay limit is clamped to that limit, never raised, and the result then carries `waitMs` and `waitClamped: true`. |
| `list_workflows` | `{ sessionId?, status?, limit? }` | `{ workflows: [...] }`. |
| `cancel_workflow` | `{ workflowId }` | Cancelled run. |
| `resume_workflow` | `{ workflowId, waitMs? }` | Restarted run; with `waitMs`, the terminal run. A wait above the relay's `A2A_RELAY_MAX_WAIT_MS` is clamped to that limit, never raised, and the result then carries `waitMs` and `waitClamped: true`. |
| `get_pressure` | `{}` | The host reading, same payload as `GET /v1/pressure`. |

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
| `role` | if the agent carries a role | Role id of the entry that runs the task. |
| `rolePromptSha256` | if the agent carries a role | First 12 hex characters of the instructions' SHA-256, refreshed at dispatch; the prompt text is never echoed. |
| `requestId` | if supplied | Echoed idempotency key. |
| `parentTaskId` | delegated tasks | Task that delegated this one. |
| `depth` | always | `0` for a root task, parent depth + 1 when delegated. |
| `startedAt` | running+ | ISO-8601. |
| `finishedAt` | terminal | ISO-8601. |
| `output` | success, `timed_out` | Captured result text; for a timeout, the text captured before the deadline. |
| `error` | failure/timeout | Failure reason. A `timed_out` task's error is `Agent exceeded <n> ms` followed by the bounded tail of the captured stdout and stderr. |
| `outputTruncated` | always on a terminal task | `true` when output hit the byte limit, `false` when nothing was cut — a `timed_out` task reports `false` when its captured output fits. |
| `cancellation` | cancelled while running | `process_signal` (adapter process group signalled) or `request_only` (HTTP request not aborted). |

## Workflow object fields

| Field | When | Notes |
| --- | --- | --- |
| `id` | always | `wf-` plus 32 hex characters. |
| `name` | always | Submitted name, else `workflow`. |
| `status` | always | Workflow status values. |
| `sessionId` | always | Shared by every task the run starts. |
| `createdAt` / `startedAt` / `finishedAt` | as applicable | ISO-8601; `startedAt` and `finishedAt` are null until reached. |
| `spec` | single run only | The document as submitted, without the internal dependency closure. |
| `error` | failed/cancelled | Names the first failed step in document order, or the first skipped step when none failed; a failed step's short cause is appended after `: `. |
| `steps[].id` / `agentId` / `needs` | always | As submitted. |
| `steps[].needsPolicy` | always | `"any"` or `"all"` as submitted; `null` when omitted (which behaves as `all`). |
| `steps[].status` | always | Step status values. |
| `steps[].attempts` | always | How many times the step has run, restored from the journal on resume. |
| `steps[].startedAt` / `steps[].finishedAt` | always | ISO-8601. `startedAt` is stamped when the relay begins the attempt — rendering and submitting its tasks — and `finishedAt` when the step reaches a terminal status, and only if it started. A step that never started (`pending`, `skipped` with `dependency_not_completed`, or `cancelled` with the run before its attempt began) keeps both null. A step whose attempt failed before submitting any task (an unresolved template reference, or a submission the relay refuses, such as `read_only_unsupported: ...`) is `failed` with both stamped and `attempts` `0`. Restored from the journal on resume. |
| `steps[].attemptTimes` | single run only | One `{startedAt, finishedAt}` window per submitted attempt, oldest first; `finishedAt` is null for an attempt still open. Empty when no attempt submitted a task: a `pending`, `skipped` or pre-launch `cancelled` step, and a failure before the first submission. Absent from `GET /v1/workflows` list entries; present in the full, `view=summary` and `?step=` run views. Restored from the journal on resume. |
| `steps[].taskIds` | single run only | Every task the step started, oldest first; empty for a resumed step that already finished. |
| `steps[].output` | completed or failed | For a `foreach` step whose tasks ran, a JSON array of the item outputs whatever their outcome (a failed item contributes its own output, empty when it produced none), in that attempt's item order. No output is set when the step fails before its tasks run: an unresolved template reference, a rejected submission, or `task_evicted_from_the_relay_store`. Because `taskIds` accumulates across retries, a `taskIds` position maps to a `foreach` item only within one attempt; the failing item's error stays in `errorDetail`. |
| `steps[].error` | not completed | `dependency_not_completed`, `repeat_limit_reached`, `output_matched_fail_pattern`, `task_evicted_from_the_relay_store`, a rejected submission message, or — when the child task failed — the short cause: the last non-empty lines of the task error and output (coding CLIs print the banner first and the reason last), capped in length, with `agent_quota: ` for diagnostic usage/rate-limit reports, quota exceeded/reached, "too many requests" or HTTP 429. Classification reads only the full task error before tail truncation and requires HTTP, status, error or code context for 429; ordinary quota prose and stdout-only limit text do not set the quota label, except on a timed-out task, whose error carries the bounded tail of both captured streams. |
| `steps[].errorDetail` | failed step, single run view only | The failing child task's `error`: its stderr when it printed some, otherwise the relay's or adapter's message for that failure (for example `Agent exited with N`, `Adapter exited with N` or `unknown_agent`); a `timed_out` task's is `Agent exceeded <n> ms` followed by the bounded tail of both captured streams. Absent when no child task failed (a step that failed before starting one, or that detected the failure itself, such as `output_matched_fail_pattern`). On a `foreach` step it is the first failing item only — `taskIds` lists every child and `GET /v1/tasks/:id` returns each one's full error. The list view never carries it. |

## Workflow journals

Each run appends to `<state dir>/workflows/<id>.jsonl`, one JSON object per line:

```jsonl
{"type":"created","at":"ISO-8601","spec":{...}}
{"type":"step","at":"ISO-8601","step":"write","status":"completed","output":"...","attempts":1,"startedAt":"ISO-8601","finishedAt":"ISO-8601","attemptTimes":[{"startedAt":"ISO-8601","finishedAt":"ISO-8601"}]}
{"type":"resumed","at":"ISO-8601"}
{"type":"finished","at":"ISO-8601","status":"completed"}
```

Replay takes the `created` document and the **last** `step` record per step id, so a
resumed run's later result wins. `resume` works after a restart because nothing else
survives: tasks live in memory only. The journal records prompts and outputs, so treat it as
task content. Journaling is best effort — an unwritable state directory disables resume for
that run but never fails the run.
