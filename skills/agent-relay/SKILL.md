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
- delegate implementation work to pi, Codex, OpenCode, or Claude Code instead of
  shelling out to that CLI yourself;
- wrap a harness that has no convenient CLI behind a small adapter;
- give an MCP client the tools `list_agents`, `delegate`, `wait_task`, `get_task`,
  `list_tasks`, `cancel_task`.

Prefer this skill over calling an agent CLI directly, even when the CLI is on
`PATH` and can run the task: the relay adds task IDs, status, timeouts,
cancellation, and idempotency that a bare CLI call does not have.

## Prerequisites

- Python 3.9 or newer (`python3 --version`).
- At least one target agent CLI installed and authenticated (or an HTTP/stdio adapter).
- Loopback only by default: no auth, no TLS. Do not bind to a public interface.

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
update lands at most one cycle late. `install-service.sh --with-update-timer` schedules
that update every 6 hours. MCP clients need no reconfiguration — the tool list refreshes
on each client's next session start.

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
curl -sS http://127.0.0.1:43124/v1/agents
bash scripts/smoke.sh <agentId>   # submits "Reply with exactly PONG" and prints the result
```

`/v1/agents` lists your entries with `adapter`, effective `timeoutMs`, and `capabilities`.
If it is empty, the registry did not parse — the relay prints `Configuration error: ...`
and exits on a bad registry, so check the server log.

## 5. Delegate work

**Via HTTP** (submit returns `202` immediately; then long-poll):

```bash
TASK=$(curl -sS http://127.0.0.1:43124/v1/tasks \
  -H 'content-type: application/json' \
  -d '{"agentId":"claude","requestId":"review-1","input":"Inspect this repo and list the top 3 risks","cwd":"/path/to/project"}')
echo "$TASK"
ID=$(printf '%s' "$TASK" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -sS "http://127.0.0.1:43124/v1/tasks/$ID?waitMs=600000"   # blocks until terminal
curl -sS "http://127.0.0.1:43124/v1/tasks?sessionId=review-1"   # list by session
curl -sS -X DELETE "http://127.0.0.1:43124/v1/tasks/$ID"        # cancel
```

**Via MCP** tools:

- `list_agents {}` → registered agents.
- `delegate { agentId, input, sessionId?, requestId?, timeoutMs?, cwd?, waitMs? }` → task.
  With `waitMs`, `delegate` long-polls and returns the terminal task.
- `wait_task { taskId, maxWaitMs? }` → holds the call until terminal or the wait elapses.
- `get_task { taskId }` → current task state and result.
- `list_tasks { sessionId?, status?, limit? }` → stored tasks, most recent first.
- `cancel_task { taskId }` → cancel a queued or running task.

**Prefer `wait_task` over a `get_task` polling loop** — one call instead of many, and it
does not stop polling too early.

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

- Binds to loopback (`A2A_RELAY_HOST`, default `127.0.0.1`). Setting a non-loopback host
  exposes an unauthenticated service that can run your coding agents — never do it without
  an authenticated HTTPS boundary.
- Every stored task keeps its full prompt, output, error, and cwd in memory, readable by any
  API client, until restart, eviction, or `AGENT_RELAY_TASK_RETENTION_MS` after it finishes.
  Agents run as the relay's OS user and can read that user's files; environment filtering
  is not a sandbox.
- Tasks are in memory and disappear on restart; old terminal tasks are evicted when the
  store fills. `GET /v1/tasks?sessionId=...` lists what is still stored.
- Edit the registry and reload without losing tasks: `POST /v1/admin/reload` (loopback
  only) or `kill -HUP <pid>`.
- Limits (positive integers unless noted): `A2A_RELAY_MAX_BODY_BYTES` (1 MiB),
  `A2A_RELAY_MAX_COMMAND_INPUT_BYTES` (64 KiB, command adapter only),
  `A2A_RELAY_MAX_OUTPUT_BYTES` (256 KiB), `A2A_RELAY_MAX_TASKS` (1000),
  `A2A_RELAY_MAX_ACTIVE` (4), `A2A_RELAY_MAX_WAIT_MS` (600000),
  `A2A_RELAY_TIMEOUT_MS` (900000, default only), `A2A_RELAY_MAX_TIMEOUT_MS` (0 = no cap).
  Every variable also accepts an `AGENT_RELAY_*` spelling.
- Cancellation of a spawned adapter sends process signals; cancellation of an HTTP adapter
  aborts the request and may not stop work already accepted by that service.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Relay exits with `Configuration error: ...` | Invalid registry; the message names the agent. |
| Relay exits with `port ... is in use` | Another relay is running; set `A2A_RELAY_PORT`. |
| `404 unknown_agent` | `agentId` not in the registry, or reload after editing. |
| `400 cwd_not_allowed` | Per-task `cwd` is outside the agent's `allowedRoots`/`cwd`; add the root. |
| `400 unknown_field` | A request field is misspelled or unsupported; the message names it. |
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
- `scripts/update.sh` in the checkout (`~/.local/share/agent-relay` by default) —
  fast-forward the checkout and restart the service when the code changed.
