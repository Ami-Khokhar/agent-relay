---
name: agent-relay
description: >-
  Delegate or orchestrate work across coding agents — pi, Codex, OpenCode, Claude Code —
  through agent-relay. Use when the user asks to delegate, hand off, orchestrate, fan out,
  run agents in parallel, get a second opinion, or use a specific model through another
  agent (for example "have pi do X", "ask codex", "use deepseek via pi"). Prefer this over
  running pi, codex, opencode, or claude in a shell: the relay tracks task IDs, status,
  timeouts, cancellation, and sessions. Also covers starting and registering agents when
  the relay is not running.
---

# Agent Relay

`agent-relay` is a small local service (Python 3.9+, zero dependencies) that lets one
agent hand a **new** task to another agent, then wait on, inspect, or cancel it. The relay
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
- declare that chain once, as a workflow the relay runs and can resume after a failure;
- delegate implementation work to pi, Codex, OpenCode, or Claude Code instead of
  shelling out to that CLI yourself;
- wrap a harness that has no convenient CLI behind a small adapter;
- give an MCP client the six task tools (`list_agents`, `delegate`, `wait_task`, `get_task`,
  `list_tasks`, `cancel_task`), the six workflow tools, and `get_pressure`; `tools/list` is
  authoritative.

Prefer this skill over calling an agent CLI directly, even when the CLI is on
`PATH` and can run the task: the relay adds task IDs, status, timeouts,
cancellation, and idempotency that a bare CLI call does not have.

## Prerequisites

- Python 3.9 or newer (`python3 --version`).
- At least one target agent CLI installed and authenticated (or an HTTP/stdio adapter).
- Loopback only by default, with a bearer token and no TLS. Do not bind to a public interface.

## 1. Get the source

Run the bundled setup script (idempotent). The path is relative to the repository root;
from inside this skill's directory it is `scripts/setup.sh`:

```bash
bash skills/agent-relay/scripts/setup.sh                   # clones to ~/.local/share/agent-relay by default
bash skills/agent-relay/scripts/setup.sh --install-skill   # also link the skill into agent skill dirs
```

It verifies Python 3.9+, locates an existing checkout or clones the repo to a stable
per-user path, scaffolds the registry, runs a self-test, and prints copy-paste MCP
registration for each client. `--install-skill` symlinks `skills/agent-relay` into
`~/.claude/skills/`, `~/.codex/skills/`, and `~/.agents/skills/`.

Override with env vars: `AGENT_RELAY_SOURCE` (use an existing checkout),
`AGENT_RELAY_DIR` (install dir, default `~/.local/share/agent-relay`),
`AGENT_RELAY_REPO` (git URL), `AGENT_RELAY_INSTALL_SKILL=1`.

To do it manually:

```bash
git clone https://github.com/Ami-Khokhar/agent-relay.git ~/.local/share/agent-relay
mkdir -p ~/.config/agent-relay
cp ~/.local/share/agent-relay/config/agents.example.json ~/.config/agent-relay/agents.json
```

## 2. Write the agent registry

The relay reads, in order: `A2A_AGENTS_FILE` when set, then
`~/.config/agent-relay/agents.json` when it exists, then `config/agents.json` in the
checkout. Each entry is one target agent. Choose the adapter by how the harness is invoked:

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
    { "id": "codex",    "name": "Codex",       "command": "codex",    "args": ["exec", "--skip-git-repo-check", "-s", "workspace-write"], "cwd": "/path/to/git/project", "allowedRoots": ["/path/to/git/project"] },
    { "id": "pi",       "name": "Pi",          "command": "pi",       "args": ["--print"], "cwd": "/path/to/project" },
    { "id": "opencode", "name": "OpenCode",    "command": "opencode", "args": ["run"],     "cwd": "/path/to/project" },
    { "id": "dsh",      "name": "DeepSeek",    "command": "dsh",      "args": ["--profile", "headless"] },
    { "id": "wrapped",  "name": "Any harness", "type": "stdio", "command": "python3", "args": ["/abs/adapter.py"], "cwd": "/path/to/project" },
    { "id": "hosted",   "name": "Hosted",      "type": "http",  "url": "http://127.0.0.1:9000/run" }
  ]
}
```

Per-agent optional fields:

- `timeoutMs` — this agent's timeout. It is the effective value; the global
  `A2A_RELAY_TIMEOUT_MS` is only the default for agents that omit it. Set
  `A2A_RELAY_MAX_TIMEOUT_MS` for a hard cap (a per-agent value above it is a startup error).
- `cwd` — default working directory, and always an allowed per-task target.
- `allowedRoots` — directories a caller may pass as a per-task `cwd`. A `cwd` outside every
  root is rejected with `400 cwd_not_allowed`.
- `env` / `inheritEnv` — extra child environment, or names of relay env vars to pass
  through (for example a credential).
- `description` — free text shown by `GET /v1/agents`.
- `delegateTo` — agent IDs this agent's tasks may delegate to; `[]` forbids delegation. Omit
  to allow any registered agent.
- `options` — named per-task options, each value mapped to fixed args, for example
  `"options": {"effort": {"low": ["-c", "model_reasoning_effort=low"], "high": ["-c", "model_reasoning_effort=high"]}}`.
  A task picks values with `"options": {"effort": "high"}`; the relay runs the agent's `args`,
  then the mapped args, then the input. `command` and `stdio` agents only. See
  "Per-task options" below.
- `readOnly` — what a read-only task means for this agent:
  `{"args": ["--exclude-tools", "edit,write"], "sandbox": true}`. `args` are added after the
  agent's args and options; `sandbox: true` (macOS) runs the agent under `sandbox-exec` so it
  cannot write under the task's `cwd`, the agent's `allowedRoots` or the git metadata of the
  `cwd` (a worktree's too), read the relay token, or connect to the relay port. Must set `args`, `sandbox`, or both. `command` and `stdio` agents
  only. A `command` profile may add `"inputSeparator": "--"`, which the relay puts before the
  input so the input cannot be read as an option. See "Read-only tasks" below.
- `capabilities` may only declare what is true; the relay rejects `nativeSessions: true`,
  `streaming: true`, and `newTasks: false` in this version.

Codex note: `codex exec` fails outside a git repo without `--skip-git-repo-check`, and it
runs read-only unless `-s workspace-write` is added. Both flags are shown above (checked
against codex-cli 0.155.1).

**Security:** command/stdio agents inherit only basic env vars (`PATH`, `HOME`, `USER`,
`SHELL`, `TMPDIR`, `LANG`, `LC_ALL`, Windows equivalents). To pass a credential, list its
name in that agent's `inheritEnv` — do not inline secrets in the registry.

## 3. Start the relay

```bash
python3 src/server.py     # HTTP API on http://127.0.0.1:43124
python3 src/mcp_server.py # MCP stdio server, in a second process
```

The HTTP service is the core; MCP is a thin client over it. You do not have to start HTTP
yourself: when the MCP process cannot reach a loopback relay it starts `server.py`
detached (log: `~/.local/state/agent-relay/relay.log`). To keep it up across reboots, run
`bash scripts/install-service.sh`. `SIGTERM` or `SIGINT` cancels running tasks and waits
briefly for adapters to stop.

To pick up new features merged on the project's `main`, run `bash
~/.local/share/agent-relay/scripts/update.sh` (or `scripts/update.sh` from the checkout):
it fast-forwards the checkout and restarts the service only when the code changed. The
first restart is deferred while tasks are in flight (or while the relay cannot report
counts), and the next run escalates — restarting unless tasks are visibly busy — so an
update lands at most one cycle late. `install-service.sh` also schedules that update every
6 hours when the install directory is a git clone; pass `--no-update-timer` to opt out
(`--with-update-timer` is still accepted and does nothing). MCP clients need no
reconfiguration — the tool list refreshes on each client's next session start.

Register the MCP server in the orchestrating client (point `args` at the absolute path).
The setup script prints these for your checkout:

```bash
claude mcp add --scope user agent-relay \
  -e A2A_RELAY_URL=http://127.0.0.1:43124 \
  -- python3 /abs/path/to/agent-relay/src/mcp_server.py
```

```json
{ "mcpServers": { "agent-relay": {
  "command": "python3",
  "args": ["/abs/path/to/agent-relay/src/mcp_server.py"],
  "env": { "A2A_RELAY_URL": "http://127.0.0.1:43124" }
} } }
```

Codex uses `[mcp_servers.agent-relay]` in `~/.codex/config.toml`; OpenCode uses an
`"mcp"` entry in `opencode.json`. See the README for both.

## 4. Verify

```bash
curl -sS http://127.0.0.1:43124/healthz
curl -sS -H "authorization: Bearer $(cat ~/.config/agent-relay/token)" http://127.0.0.1:43124/v1/agents
bash scripts/smoke.sh <agentId>   # submits "Reply with exactly PONG" and prints the result
```

`/v1/agents` lists your entries with `adapter`, effective `timeoutMs`, and `capabilities`.
`/healthz`, `/v1/agents`, and the `list_agents` tool also report `revision`: the commit
(`commit`, `short`) and commit `date` of the checkout the relay started from. A `date` weeks
old means the running relay is stale: run `scripts/update.sh`. A field is `"unknown"` when
the relay does not run from a git checkout. If the list is empty, the registry did not parse — the relay prints `Configuration error: ...`
and exits on a bad registry, so check the server log.

## 5. Delegate work

**Via HTTP** (submit returns `202` immediately; then long-poll):

```bash
AUTH="authorization: Bearer $(cat ~/.config/agent-relay/token)"   # created on first start
TASK=$(curl -sS -H "$AUTH" http://127.0.0.1:43124/v1/tasks \
  -H 'content-type: application/json' \
  -d '{"agentId":"claude","requestId":"review-1","input":"Inspect this repo and list the top 3 risks","cwd":"/path/to/project"}')
echo "$TASK"
ID=$(printf '%s' "$TASK" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -sS -H "$AUTH" "http://127.0.0.1:43124/v1/tasks/$ID?waitMs=600000"   # blocks until terminal
curl -sS -H "$AUTH" "http://127.0.0.1:43124/v1/tasks?sessionId=review-1"   # list by session
curl -sS -H "$AUTH" -X DELETE "http://127.0.0.1:43124/v1/tasks/$ID"        # cancel
```

**Via MCP** tools:

- `list_agents {}` → registered agents.
- `delegate { agentId, input, sessionId?, requestId?, timeoutMs?, cwd?, options?, readOnly?, waitMs? }` → task.
  With `waitMs`, `delegate` long-polls and returns the terminal task, or the current task
  with `waitExpired: true` when the wait ends first. A `waitMs` above the relay limit creates
  no task: `delegate` retries once with the limit (never longer than asked) and adds `waitMs`
  and `waitClamped: true` to the result.
- `wait_task { taskId, maxWaitMs? }` → holds the call until terminal or the wait elapses
  (default 60000 ms).
- `get_task { taskId, includeInput? }` → current task state and result.
- `list_tasks { sessionId?, status?, limit? }` → stored tasks, most recent first.
- `cancel_task { taskId }` → cancel a queued or running task.
- `run_workflow { spec, waitMs? }` → submit a whole multi-step run; `spec` is the workflow
  document, and `tools/list` types its steps, so you can author one from the schema.
- `get_workflow { workflowId, maxWaitMs? }` → the run with every step, its output, and its
  `startedAt` / `finishedAt` window (plus `attemptTimes`, one window per attempt).
- `wait_workflow_events { sessionId, after?, maxWaitMs? }` → the session's ordered change
  feed: each step or run status change with `workflowId`, `stepId`, `oldStatus`, `newStatus`
  and a rising `seq`. Pass the last `seq` seen as `after`; an empty `events` list means the
  wait ended with no change, so call again with the same `after`.
- `list_workflows { sessionId?, status?, limit? }` → stored runs, most recent first.
- `cancel_workflow { workflowId }` → cancel the run and every task it started.
- `resume_workflow { workflowId, waitMs? }` → continue a journaled run, keeping the steps
  that already completed.
- `get_pressure {}` → the host reading: `verdict` (`ok`, `warn`, `critical`), `reasons`,
  memory, swap, any thermal throttle, and the `guard` section (`enabled`, `stopsAt`, and
  `holdMs`, how long pressure must stay critical before a running run is stopped). Call it
  before a long run.

Task objects leave out `input` and carry `inputChars` (its length) instead, because you
already hold the prompt you sent. Pass `includeInput: true` to `get_task` (or
`?includeInput=true` on `GET /v1/tasks/:id`) only when you need the prompt back.

**Per-task options.** `list_agents` shows each agent's `options`: names and the values the
owner allows. Pass `options: {"effort": "high"}` to `delegate` (or in the HTTP body) to pick
one. A name or value the agent does not allow is refused (`400 unknown_option` or
`400 invalid_option_value`) and no task is created; do not invent values. Options are part of
the `requestId` comparison, so a retry must send the same options. The relay checks the options
again when a queued task is dispatched: if the owner reloaded the registry meanwhile and the
option is gone, the task ends `failed` with that code at the start of its error (for example
`unknown_option: ...`) and never runs.

**Read-only tasks.** Pass `readOnly: true` when the agent must not change files (a code review
in a worktree, for example); do not rely on the prompt alone. Call `list_agents` first and pick
an agent that shows `readOnly: supported` (`readOnlySandboxed: true` means the relay also runs it
in the macOS sandbox). An agent that is `unsupported` refuses the task
(`400 read_only_unsupported`) and nothing runs. A sandboxed agent needs a `cwd` on the task or
the agent (`400 cwd_required`). The input of a `command` agent is its last argument: if the agent's profile
has `args` and no `inputSeparator`, a read-only input that starts with `-` is refused
(`400 invalid_input`), because it could override the profile's args (`--access=write` after
`--access=read`); start the input with another character. A `stdio` agent is not affected.
`readOnly` is part of the `requestId` comparison. The checks run
again when a queued task is dispatched: if a registry reload meanwhile removed the profile or made
the agent an `http` adapter, or removed the `inputSeparator` that made the input safe, the task ends
`failed` with `read_only_unsupported: ...` (or `invalid_input: ...`) and never runs.

What the sandbox blocks: writes under the task's `cwd` and the agent's `allowedRoots` (a new hard
link to a file there too); writes to the git metadata of the `cwd`, which for a git worktree lives
outside the `cwd` (its own `.git/worktrees/<name>` with the index, and the shared `.git` with refs
and objects, or a bare repository's own directory), so a review cannot stage, commit or move a
branch (`git log`, `diff` and `show` still work); renaming or deleting any directory above those trees (the ancestor rule: otherwise
`/tmp/review` could be moved away and written through its new path; writes beside a protected tree
stay allowed); reading the relay token file, or renaming it or a directory above it;
connecting to the relay's TCP port, so the agent cannot call the relay; and the
`AGENT_RELAY_TOKEN` / `A2A_RELAY_TOKEN` variables, which a sandboxed task never receives. A
sandboxed agent therefore cannot delegate through the relay.

The git lookup fails closed: if a `.git` file or directory (or a directory with `HEAD`, `objects`
and `refs`, as a bare repository has) exists at or above the `cwd` and git cannot say where the
metadata is (missing, slower than five seconds or the task's remaining time, non-zero exit,
unusable output), the task ends `failed` with an error that starts `read_only_git_unknown:` and
does not run. A `cwd` with no such sign above it runs without git. The lookup runs outside the
sandbox, so the relay runs `/usr/bin/git` (or the owner's `AGENT_RELAY_GIT`, an absolute path set
in the relay's environment) by absolute path, never through `PATH`, in a minimal environment
(`PATH=/usr/bin:/bin`, `HOME`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`). It does not
read the global git config, which a read-only task could have written, so `safe.directory` there
does not help: fix the directory's ownership, or submit without `readOnly`.

What it does not block: a hard link that already exists outside the tree and points into it;
writes made for the agent by another local service it can reach (for example a Docker socket, a
daemon on another port, a proxy in front of the relay); reads of any other file; other network
access (the model API still works); writes outside the protected directories (so a task can plant
a file another program trusts later, such as an executable on your `PATH` or `~/.gitconfig`; the
relay's own git ignores both); and, when the relay
took its token from `AGENT_RELAY_TOKEN` and not from the token file, reading the relay process's
environment. It is not general confinement.

Example registry entries (flags checked against codex-cli 0.162.1 and pi 1.0.4):

```json
{ "agents": [
  { "id": "codex", "name": "Codex", "command": "codex", "args": ["exec", "--skip-git-repo-check"],
    "cwd": "/path/to/git/project", "allowedRoots": ["/path/to/git/project"],
    "options": {
      "effort": { "low": ["-c", "model_reasoning_effort=low"], "high": ["-c", "model_reasoning_effort=high"] },
      "access": { "write": ["-s", "workspace-write"] }
    },
    "readOnly": { "args": ["--sandbox", "read-only"] } },
  { "id": "pi", "name": "Pi", "command": "pi", "args": ["--print"],
    "cwd": "/path/to/project", "allowedRoots": ["/path/to/project"],
    "readOnly": { "args": ["--exclude-tools", "edit,write"], "sandbox": true } }
] }
```

Codex rejects a repeated `--sandbox`/`-s`, so its entry leaves `-s` out of `args` (read-only is
its default) and offers `-s workspace-write` as the `access` option; a task that edits files
passes `options: {"access": "write"}`. Codex applies its own sandbox, and macOS does not nest
sandboxes, so the Codex entry uses `args` only and no `"sandbox": true`.

**Prefer `wait_task` over a `get_task` polling loop** — one call instead of many, and it
does not stop polling too early.

**Tell "finished" from "my wait ended".** When the wait ends and the task is still not
terminal, the result has `waitExpired: true`, `waitedMs` (the wait that elapsed) and a
`hint`: call `wait_task` again with the same `taskId`. A terminal result has no
`waitExpired`. Loop while `waitExpired` is true; do not read a `running` status as a
finished task. The same fields appear on the HTTP `?waitMs=` path and on a `delegate`
call that used `waitMs`.

**Keep `maxWaitMs` short in clients with a tool-call budget.** Claude Code moves an MCP
call that runs longer than 120 s to the background, and its "completed" notice then
carries a task that is still `running`. In Claude Code use `maxWaitMs` of about 110000
or less and loop on `waitExpired`.

A `maxWaitMs` above the relay's limit (`A2A_RELAY_MAX_WAIT_MS`, default 600000, shown as
`limits.maxWaitMs` in `/healthz`) is cut to that limit by the MCP `wait_task` and `delegate` tools. The result then
has `waitMs` (the wait used) and `waitClamped: true`. Over HTTP a `waitMs` outside
`1..limit` returns `400 invalid_wait`, and its message names the range. This holds for
`POST /v1/tasks` too: the relay checks `waitMs` before it creates the task, so a refused
submission leaves no task behind.

## Task lifecycle

Statuses: `queued` → `running` → one of `completed`, `failed`, `timed_out`, `cancelled`.

Result fields on a terminal task: `output` (success text), `error` (failure reason),
`outputTruncated` (bool). `sessionId` is **correlation data only** — it does not resume a
native harness session, and each task is a fresh invocation.

Idempotency: send a unique `requestId`; reusing it with the same fields returns the
original task (`200`), while reusing it with different fields returns `409`. Use it when a
submission response may have been lost. Without a `requestId`, do not auto-retry.

## Workflow lifecycle

A workflow document names steps; each step is an ordinary task on one registered agent, and
`needs` wires them together. The relay starts a step only once every step it needs has
completed. Statuses: `queued` → `running` → one of `completed`, `failed`, `cancelled`. A
step moves `pending` → `running` → one of `completed`, `failed`, `skipped`, `cancelled`,
where `skipped` means a step it depended on did not succeed. Every step carries `startedAt`
and `finishedAt`, null until it starts and finishes, and a run view adds `attemptTimes`, one
`{startedAt, finishedAt}` window per attempt — so a report can give each step its own time
window without reading task ids.

```
{"name": "review-loop", "steps": [
  {"id": "write",  "agentId": "claude", "input": "Fix the failing test"},
  {"id": "review", "agentId": "codex",  "needs": ["write"],
   "input": "Review this change and report findings:\n\n{{write.output}}"},
  {"id": "prove",  "agentId": "claude", "needs": ["review"],
   "input": "Address the review of:\n\n{{write.output}}"}]}
```

Inputs are templates. `{{stepId.output}}` (or bare `{{stepId}}`) resolves against a step in
that step's transitive `needs`; `{{item}}` and `{{index}}` resolve against a `foreach`
expansion. A `foreach` step's own `{{stepId.output}}` reads as a JSON array of its item
outputs. A reference to a step that is not an ancestor is refused at submit time, so a run
cannot read a sibling that has not finished.

`foreach` fans one step into N tasks, `retries` absorbs task failures (`0`–`5`), and `repeat`
with `untilOutputMatches` loops until the output matches a regular expression, failing with
`repeat_limit_reached` when `max` is spent. `DELETE /v1/workflows/:id` cancels the run and
every task it started. A run that does not finish inside `waitMs` returns the same
`waitExpired` shape a task wait does.

To learn about whichever run in a session changes first — an orchestrator running several
workflows — read the session's change feed instead of polling each run:
`GET /v1/workflows/events?sessionId=<id>&after=<seq>&waitMs=<ms>` (or the
`wait_workflow_events` tool) long-polls and returns each step or run status change in order:
`workflowId`, `stepId` (`null` for a run-level change), `oldStatus`, `newStatus`, and a
rising `seq`. Pass the last `seq` seen as `after`; an empty `events` list means the wait
ended with no change.

**Host pressure.** `GET /v1/pressure` reads what this machine can tell you without root: the
kernel's memory-pressure level, free-memory percentage, swap in use, and any thermal speed limit.
It returns a `verdict` of `ok`, `warn` or `critical` with the `reasons` behind it, and
`temperatureC` is null because `powermetrics` needs root. A `critical` verdict refuses a new
workflow with `503 device_pressure`, and while a run is active the relay re-reads the host every
`AGENT_RELAY_PRESSURE_INTERVAL_MS` (default 5000) and stops every active run once the verdict has
stayed `critical` for `AGENT_RELAY_PRESSURE_HOLD_MS` milliseconds (default 30000, read at least
twice), so a single spike between normal readings does not end a run;
`AGENT_RELAY_PRESSURE_HOLD_MS=0` stops on the first critical reading. `POST /v1/pressure/kill` is
the same stop on demand. A stopped run is `cancelled` with `error` starting `device_pressure:`,
naming how long pressure held and the readings that decided it, and keeps its journal, so
`POST /v1/workflows/:id/resume` continues it without repeating the steps that finished.
`AGENT_RELAY_PRESSURE_GUARD=0` turns the automatic stop off and leaves the endpoints in place.

Runs journal to `<state dir>/workflows/<id>.jsonl` (`AGENT_RELAY_STATE_DIR`, default
`~/.local/state/agent-relay`), so `POST /v1/workflows/:id/resume` continues a failed or interrupted run without
repeating the steps that already completed.
The journal holds the prompts and results, so treat it as task content. Full endpoint and
object reference: [references/api.md](references/api.md).

## Designing for the user's use case

1. **Identify the target agent(s)** the user means, and confirm their CLIs exist (`which`).
2. **Pick the adapter**: `command` for a simple prompt-as-final-arg CLI; `stdio`/`http`
   when the harness needs a wrapper or returns structured data.
3. **Register** them in the registry, set `cwd` to the project each should edit, and add
   `allowedRoots` if the caller must target more than one project directory.
4. **Choose limits**: `timeoutMs` per task/agent (default 15 minutes), and the global
   concurrency via `A2A_RELAY_MAX_ACTIVE` (default 4; extra tasks queue).
5. **Submit, wait, and present** the `output`/`error` to the user. Cancel if they change
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
diagnostics to stderr. Copy `examples/stdio_adapter.py` and replace `run_harness`. The
relay rejects malformed envelopes, non-string `output`/`error`, and failures without an
`error`. See [references/adapters.md](references/adapters.md) for full rules and a template.

## Operating limits and security

- Single-user local service. Every `/v1/` route needs the bearer token from
  `~/.config/agent-relay/token` (created `0600` on first start; the MCP tools send it). Anyone
  with the token can run every agent and read every stored task; `sessionId` is not access
  control. Browser `Origin`s and non-JSON task submissions are rejected.
- Binds to loopback (`A2A_RELAY_HOST`, default `127.0.0.1`) with no TLS. A non-loopback host
  is refused unless `AGENT_RELAY_UNSAFE_ALLOW_NON_LOOPBACK=1`; use that only behind an
  authenticated HTTPS proxy.
- `http` adapters receive the full prompt. Cleartext `http://` to a non-loopback host needs
  `"allowInsecureHttp": true`; adapter redirects are never followed.
- Recursive delegation is supported under a policy: agents the relay spawns get
  `AGENT_RELAY_PARENT_TASK_ID`, `delegate` forwards it, and the relay enforces per-agent
  `delegateTo`, rejects cycles, and caps depth (`AGENT_RELAY_MAX_DELEGATION_DEPTH`, 2) and
  tasks per tree (`AGENT_RELAY_MAX_DELEGATED_TASKS`, 20). It is cooperative: do not give the
  relay's MCP server or API access to an agent that must not delegate.
- Every stored task keeps its full prompt, output, error, and cwd in memory, readable by any
  token holder (including agents the relay started, which can read the token file, except a
  sandboxed read-only task, which cannot read it or call the relay), until
  restart, eviction, or `AGENT_RELAY_TASK_RETENTION_MS` after it finishes (checked on every
  `/v1/` request and at least every minute; expiry also releases the task's `requestId`).
  Agents run as the relay's OS user and can read that user's files; environment filtering
  is not a sandbox.
- Tasks are in memory and disappear on restart; old terminal tasks are evicted when the
  store fills. `GET /v1/tasks?sessionId=...` lists what is still stored.
- Edit the registry and reload without losing tasks: `POST /v1/admin/reload` (token,
  loopback only) or `kill -HUP <pid>`.
- Limits (positive integers unless noted; `0` is allowed where shown): `A2A_RELAY_MAX_BODY_BYTES` (1 MiB),
  `A2A_RELAY_MAX_COMMAND_INPUT_BYTES` (64 KiB, command adapter only),
  `A2A_RELAY_MAX_OUTPUT_BYTES` (256 KiB), `A2A_RELAY_MAX_TASKS` (1000),
  `A2A_RELAY_MAX_ACTIVE` (4), `A2A_RELAY_MAX_WAIT_MS` (600000),
  `A2A_RELAY_TIMEOUT_MS` (900000, default only), `A2A_RELAY_MAX_TIMEOUT_MS` (0 = no cap),
  `AGENT_RELAY_TASK_RETENTION_MS` (0 = keep finished tasks until eviction or restart),
  `AGENT_RELAY_MAX_DELEGATION_DEPTH` (2; 0 = spawned agents may not delegate),
  `AGENT_RELAY_MAX_DELEGATED_TASKS` (20).
  Every variable also accepts an `AGENT_RELAY_*` spelling.
- Cancellation of a spawned adapter signals its process group; cancellation of an HTTP
  adapter is `request_only`: the request is not aborted and the service may keep working.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Relay exits with `Configuration error: ...` | Invalid registry; the message names the agent. |
| Relay exits with `AGENT_RELAY_GIT must be an absolute path to an executable file` | The owner's git override is relative, missing, a directory or not executable. Fix or unset `AGENT_RELAY_GIT` in the relay's environment. |
| Relay exits with `port ... is in use` | Another relay is running; set `A2A_RELAY_PORT`. |
| `404 unknown_agent` | `agentId` not in the registry, or reload after editing. |
| `400 cwd_not_allowed` | Per-task `cwd` is outside the agent's `allowedRoots`/`cwd`; add the root. |
| `400 invalid_wait` | `waitMs` is not an integer from 1 to `A2A_RELAY_MAX_WAIT_MS`; the message names the range. The MCP `wait_task` and `delegate` clamp instead. |
| `400 unknown_field` | A request field is misspelled or unsupported; the message names it. |
| `400 read_only_unsupported` / `cwd_required` / `invalid_read_only` | `readOnly: true` was sent to an agent without a readOnly profile (or whose sandbox needs macOS), to a sandboxed agent with no `cwd`, or `readOnly` is not a boolean; pick an agent that `list_agents` shows as `readOnly: supported`. |
| `400 invalid_input` | A `readOnly` task's input starts with `-` and the `command` agent's profile has `args` but no `inputSeparator`, so the input could be read as an option. Start the input with another character, or ask the owner to add `inputSeparator` (for example `"--"`) to the profile. |
| `400 unknown_option` / `invalid_option_value` / `invalid_options` | `options` names an option the agent does not declare, a value it does not allow, or is not an object of strings; check `list_agents`. |
| Task `failed`, error starts `read_only_git_unknown:` | A `readOnly` sandboxed task's `cwd` is in a git repository but git could not locate its metadata, so the relay did not run it with less protection. Fix git for that directory (the relay ignores the global git config, so `safe.directory` there does not apply) or submit without `readOnly`. |
| Task `failed`, error starts with a submission code (`read_only_unsupported:`, `unknown_option:`, `cwd_required:` ...) | The registry was reloaded while the task was queued and the new entry no longer allows what the task asked for. Resubmit against the current `list_agents`. |
| `413 command_input_too_large` | Input exceeds `A2A_RELAY_MAX_COMMAND_INPUT_BYTES`; use a `stdio` adapter for large prompts. |
| `503 task_capacity_reached` | Store full of non-terminal tasks; raise `A2A_RELAY_MAX_TASKS`/`A2A_RELAY_MAX_ACTIVE` or wait. |
| `409 idempotency_conflict` | Same `requestId` reused with different fields; use a new ID. |
| `401 unauthorized` | Send `Authorization: Bearer` with `~/.config/agent-relay/token`, or the same `AGENT_RELAY_TOKEN` the relay uses. |
| `403 origin_not_allowed` | The request sent an `Origin` not in `AGENT_RELAY_ALLOWED_ORIGINS`; CLI and MCP clients should not send one. |
| `403 delegation_not_allowed` | The delegating agent's `delegateTo` (see `GET /v1/agents`) excludes the target, or that agent was removed from the registry. |
| `409 delegation_cycle` | The target is already in this delegation chain; pick another agent or do the work yourself. |
| `409 delegation_depth_exceeded` | The chain is at `AGENT_RELAY_MAX_DELEGATION_DEPTH`; do the work in this task or raise the limit. |
| `429 delegation_budget_exhausted` | This task tree used `AGENT_RELAY_MAX_DELEGATED_TASKS`; finish with the results so far or raise the limit. |
| Task `failed` with adapter errors | Harness exit non-zero or bad output; check stderr by running the command adapter manually. |
| Task `timed_out` | Exceeded `timeoutMs`; raise it or shorten the work. |
| MCP tool returns `isError: true` | Relay unreachable or returned an error; the payload carries `error`/`status`. |

## Reference

- [references/api.md](references/api.md) — full HTTP and MCP surface, fields, and errors.
- [references/adapters.md](references/adapters.md) — adapter contract, template, env vars.
- [references/use-cases.md](references/use-cases.md) — single delegation, fan-out, pipelines.
- [scripts/setup.sh](scripts/setup.sh) — idempotent setup.
- `scripts/update.sh` in the checkout (`~/.local/share/agent-relay` by default) —
  fast-forward the checkout and restart the service when the code changed.
