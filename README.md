# Local agent relay

One agent can submit a task to another agent through a small local HTTP service. The relay owns task IDs, status, cancellation, timeouts, and a registry of adapters. An MCP stdio entry point exposes the same service as tools to orchestrating agents.

## Run

Python 3.9 or newer is required. There are no package dependencies.

```bash
python3 src/server.py
```

The HTTP API listens on `http://127.0.0.1:43124` by default. Every `/v1/` request needs the API token (see [Local security model](#local-security-model)); the relay creates it on first start at `~/.config/agent-relay/token`, and the MCP interface and `scripts/smoke.sh` read it from there. Start the MCP interface in a second process when the orchestrating client supports MCP:

```bash
python3 src/mcp_server.py
```

Configure that command as a local stdio MCP server in the client. It provides `list_agents`, `delegate`, `wait_task`, `get_task`, `list_tasks`, and `cancel_task`. `A2A_RELAY_URL` overrides the HTTP address it calls. When the relay is unreachable and the URL is loopback, the MCP process starts the HTTP service detached on first use and logs it to `~/.local/state/agent-relay/relay.log`; disable that with `A2A_RELAY_AUTOSTART=0`.

Register the MCP server once with copy-paste commands (use absolute paths; `bash skills/agent-relay/scripts/setup.sh` prints these for your checkout):

```bash
# Claude Code
claude mcp add --scope user agent-relay \
  -e A2A_RELAY_URL=http://127.0.0.1:43124 \
  -- python3 /abs/path/to/agent-relay/src/mcp_server.py
```

```toml
# Codex: ~/.codex/config.toml
[mcp_servers.agent-relay]
command = "python3"
args = ["/abs/path/to/agent-relay/src/mcp_server.py"]
env = { A2A_RELAY_URL = "http://127.0.0.1:43124" }
```

```json
// OpenCode: opencode.json
{ "mcp": { "agent-relay": { "type": "local",
  "command": ["python3", "/abs/path/to/agent-relay/src/mcp_server.py"],
  "environment": { "A2A_RELAY_URL": "http://127.0.0.1:43124" }, "enabled": true } } }
```

To keep the HTTP service up across reboots, install it as a per-user service:

```bash
bash scripts/install-service.sh          # launchd on macOS, systemd --user on Linux
bash scripts/install-service.sh --uninstall
```

A service does not inherit your login shell `PATH`. The installer writes a `PATH` that includes `~/.local/bin`, `~/bin`, `/opt/homebrew/bin`, and `/usr/local/bin` so agent CLIs resolve; override with `AGENT_RELAY_PATH`.

## Register an agent

The relay reads, in order: `A2A_AGENTS_FILE` (or `AGENT_RELAY_AGENTS_FILE`) when set, then `~/.config/agent-relay/agents.json` when it exists, then `config/agents.json` in the checkout. Copy `config/agents.example.json`, edit it, and reload (see below). Use a `stdio` or `http` adapter for harness-independent integration. A `command` adapter remains available as a convenience for CLIs that accept a prompt as their final argument and print the result.

```json
{
  "agents": [
    { "id": "claude", "name": "Claude Code", "command": "claude", "args": ["--print"], "cwd": "/path/to/project" },
    { "id": "codex", "name": "Codex", "command": "codex", "args": ["exec", "--skip-git-repo-check", "-s", "workspace-write"], "cwd": "/path/to/git/project", "allowedRoots": ["/path/to/git/project", "/path/to/another/project"] },
    { "id": "pi", "name": "Pi", "command": "pi", "args": ["--print"], "cwd": "/path/to/project" },
    { "id": "opencode", "name": "OpenCode", "command": "opencode", "args": ["run"], "cwd": "/path/to/project" },
    { "id": "wrapped", "name": "Any wrapped harness", "type": "stdio", "command": "python3", "args": ["/path/to/adapter.py"], "cwd": "/path/to/project" },
    { "id": "hosted", "name": "Harness on its own port", "type": "http", "url": "http://127.0.0.1:9000/run" }
  ]
}
```

Per-agent fields:

| Field | Notes |
| --- | --- |
| `timeoutMs` | Timeout for this agent's tasks. It is no longer capped by the global default. |
| `cwd` | Working directory when no per-task `cwd` is given. It is always an allowed per-task target. |
| `allowedRoots` | Directories a caller may select with a per-task `cwd`. A per-task `cwd` outside every root is rejected with `400 cwd_not_allowed`. |
| `env` / `inheritEnv` | Extra environment for the child, or names of relay variables to pass through. |
| `description` | Free text shown by `GET /v1/agents`. |
| `url` | Endpoint of an `http` adapter. It receives the full prompt; use `https://` for any non-loopback host. |
| `allowInsecureHttp` | Set `true` to allow a cleartext `http://` `url` on a non-loopback host; otherwise the registry is rejected. |
| `delegateTo` | Agent IDs this agent's tasks may delegate to; `[]` forbids delegation. See [Recursive delegation](#recursive-delegation). |

Codex notes: `codex exec` refuses to run outside a git repository unless `--skip-git-repo-check` is given, and it uses a read-only sandbox unless `-s workspace-write` is added. Both flags are in the example above (checked against codex-cli 0.155.1). Other example flags are entry points; verify them against your installed CLI versions.

The relay only starts new tasks; continuing an existing agent session needs a native adapter for that agent. Freebuff's current installed CLI does not advertise a noninteractive output mode, so it is not registered as an automatic command adapter.

`stdio` and JSON `http` adapters share one small, harness-neutral contract. The relay sends one document:

```json
{"protocolVersion":"relay.adapter/v1","task":{"id":"relay-task-id","sessionId":"correlation-id","input":"do the work","timeoutMs":120000}}
```

The adapter returns exactly one result document:

```json
{"protocolVersion":"relay.adapter/v1","status":"completed","output":"result text"}
```

For a harness failure, return `{"protocolVersion":"relay.adapter/v1","status":"failed","error":"reason"}`. A stdio adapter reads the request from stdin, writes only the result to stdout, and sends diagnostics to stderr. See [examples/stdio_adapter.py](examples/stdio_adapter.py); replace its `run_harness` function with the harness's supported API. This lets a new harness be added through config plus a small external wrapper, without changing relay or MCP code.

An HTTP adapter accepts the same request as a `POST` and returns the same result with `content-type: application/json`. It can run on a separate localhost port for each harness. The relay still gives MCP clients one stable HTTP port and handles the common queue and task lifecycle. Separate harness ports are optional and require running those adapter services. For compatibility, an HTTP adapter response without a JSON content type is still treated as a successful raw-text result when its status is 2xx.

The `sessionId` is relay correlation data and does not claim native harness session continuity. This version advertises `nativeSessions: false` and `streaming: false`. Cancelling a spawned (`command` or `stdio`) adapter sends `SIGTERM`, then `SIGKILL` after 1 s, to its whole process group, so processes the adapter started are stopped too (on Windows only the direct process is signalled). Once the adapter process itself has exited and been reaped, the relay signals its group again only where it can confirm the group is still the task's (Linux, by the leader's start time); elsewhere descendants left behind are not signalled. A descendant that leaves the group (for example with `setsid`) is not tracked; once the adapter exits, the relay waits about 2 s for pipes such a process holds, signals the group again, waits up to 1.5 s more, then finishes the task with the output captured so far (about 3.5 s in total). Cancelling an HTTP adapter only marks the task `cancelled` and discards the result: the in-flight request is not aborted and the remote service is not told to stop. `/v1/agents` reports these as `process_signal` and `request_only`, and a cancelled running task carries the same value in its `cancellation` field. A cancelled task keeps its active slot until its process exits or its HTTP request returns, so `/healthz` `active` counts work that is still running.

### Reload the registry

Edit the registry, then reload without losing in-memory tasks:

```bash
AUTH="authorization: Bearer $(cat ~/.config/agent-relay/token)"
curl -sS -X POST -H "$AUTH" http://127.0.0.1:43124/v1/admin/reload
kill -HUP <relay-pid>
```

The reload endpoint needs the token and is accepted only from loopback. An invalid registry is rejected and the running registry is kept.

## Submit and inspect work

```bash
AUTH="authorization: Bearer $(cat ~/.config/agent-relay/token)"
curl -sS -H "$AUTH" http://127.0.0.1:43124/v1/agents
curl -sS -H "$AUTH" http://127.0.0.1:43124/v1/tasks \
  -H 'content-type: application/json' \
  -d '{"agentId":"codex","requestId":"review-2026-09-23-1","input":"Inspect this project and report the main risks","cwd":"/path/to/git/project"}'
curl -sS -H "$AUTH" 'http://127.0.0.1:43124/v1/tasks?sessionId=review-2026-09-23-1'
curl -sS -H "$AUTH" 'http://127.0.0.1:43124/v1/tasks/TASK_ID?waitMs=600000'
curl -sS -H "$AUTH" -X DELETE http://127.0.0.1:43124/v1/tasks/TASK_ID
```

Submission returns `202` with a task ID and session ID. Poll the task URL for `queued`, `running`, `completed`, `failed`, `timed_out`, or `cancelled`. `GET /v1/tasks/TASK_ID?waitMs=<ms>` long-polls until the task is terminal or the wait elapses. `GET /v1/tasks?sessionId=...&status=...&limit=...` lists stored tasks (most recent first). Supply a unique `requestId` and reuse it when retrying a submission; the relay returns the original task instead of starting duplicate work. Reusing it with different task fields returns `409`. Without a request ID, callers must not automatically retry a submission whose response was lost. The session ID currently groups work for the caller; generic command adapters do not continue native agent conversations.

## Data access and retention

- **What is stored.** Each task keeps its full `input` (the prompt), `output`, `error`, `cwd`, `sessionId`, and `requestId` in the relay's memory. Nothing is written to disk and the relay does not log task content.
- **Who can read it.** Anyone holding the API token, through `GET /v1/tasks` and `GET /v1/tasks/:id` (see [Local security model](#local-security-model)); a `sessionId` is a filter, not access control. The adapter request carries only its own task, but an agent the relay started runs as your OS user, can read the token file, and so can read every stored task.
- **When it is removed.** On relay restart; when the store reaches `A2A_RELAY_MAX_TASKS` (the oldest finished task is evicted per new submission); or, when `AGENT_RELAY_TASK_RETENTION_MS` is set, once that period has passed since the task finished. Expiry is checked on every `/v1/` request and in the background at least once a minute (or once per period, if shorter). Removing a task also releases its `requestId`, so a retry after the period starts new work: keep the period longer than your clients' retry window. The defaults (1000 tasks, 1 MiB bodies, 256 KiB output, no retention period) can hold a lot of sensitive text; if prompts or results are sensitive, set a short retention period. An installed service does not inherit your shell environment, so add the variable to its unit (`Environment=AGENT_RELAY_TASK_RETENTION_MS=...`) or plist (`EnvironmentVariables`) and restart it.
- **Environment filtering is not a sandbox.** `command` and `stdio` adapters receive only basic environment variables plus `inheritEnv`/`env`, but they run as the relay's OS user, with its permissions. An agent can read any file that user can (home directory, SSH keys, other projects). `cwd` and `allowedRoots` choose where an agent starts; they do not confine it. For isolation, use the agent's own sandbox (for example `codex -s workspace-write`), a separate OS user, or a container.

## Local security model

The relay is a **single-user local service**. Its trust boundary is one bearer token:

- Every `/v1/` route (agents, task submission, reads, listing, cancellation, reload) requires `Authorization: Bearer <token>`; otherwise it returns `401`. `/healthz` stays open and returns no task data. The token comes from `AGENT_RELAY_TOKEN`, else the file at `AGENT_RELAY_TOKEN_FILE` (default `~/. If you set `AGENT_RELAY_TOKEN`, the relay writes no token file, so give every client the same value: the MCP server's environment and, for agents that delegate, `"inheritEnv": ["AGENT_RELAY_TOKEN"]`.config/agent-relay/token`). The relay creates that file with mode `0600` on first start and refuses to start if other users can read it. The token is never returned by the API or logged. `curl -H` puts it in the process list, so on a shared machine pass it with `curl -K` instead (as `scripts/smoke.sh` does).
- Anyone holding the token is the owner: they can run every registered agent and read every stored task's `input`, `output`, `error`, and `cwd`. A `sessionId` is a filter, not an access-control boundary. There is no per-client task ownership; do not share the token with clients that must not see each other's prompts and results.
- Browser requests are refused. A request with an `Origin` header gets `403 origin_not_allowed` unless that origin is listed in `AGENT_RELAY_ALLOWED_ORIGINS` (comma-separated). The list is for non-browser clients that happen to send `Origin`: the relay sends no CORS headers and does not answer preflight `OPTIONS`, so a browser page cannot use the API even when its origin is listed. `POST /v1/tasks` requires `content-type: application/json` (`415` otherwise), so a cross-site "simple" form or `text/plain` POST cannot submit work.
- The listener binds to loopback (`A2A_RELAY_HOST`, default `127.0.0.1`), which still lets other local processes connect, so they need the token too. The relay has no TLS. It refuses a non-loopback `A2A_RELAY_HOST` unless `AGENT_RELAY_UNSAFE_ALLOW_NON_LOOPBACK=1` is set, and then prints a warning at startup. Use that only behind an authenticated HTTPS proxy.
- An `http` adapter receives the complete task prompt. The relay refuses a registry entry that sends it over cleartext `http://` to a non-loopback host unless the entry sets `"allowInsecureHttp": true`; use `https://` for any remote adapter, and register only endpoints you trust. The relay never follows adapter redirects: a `3xx` response fails the task, so the prompt only reaches the configured URL.

## Recursive delegation

An agent the relay started can delegate further. The relay supports that under a policy, applied when a submission names its parent with `parentTaskId`:

- Every spawned `command`/`stdio` adapter gets `AGENT_RELAY_PARENT_TASK_ID` (its own task ID) in its environment, and the MCP `delegate` tool forwards it automatically. Delegated tasks report `parentTaskId` and `depth`.
- `delegateTo` on an agent lists the agent IDs its tasks may delegate to (`[]` forbids delegation); otherwise `403 delegation_not_allowed`. Without `delegateTo`, any registered agent is allowed.
- A target already in the chain (`a > b > a`, or `a > a`) is refused with `409 delegation_cycle`.
- `AGENT_RELAY_MAX_DELEGATION_DEPTH` (default `2`; `0` forbids delegation) caps levels below the root task (`409 delegation_depth_exceeded`), and `AGENT_RELAY_MAX_DELEGATED_TASKS` (default `20`) caps the tasks one root's tree can create (`429 delegation_budget_exhausted`).

The lineage is cooperative, not a sandbox: a child that can reach the relay chooses its own `parentTaskId`. Omitting it starts a new root task outside these limits, and naming another stored task (task IDs are visible through `GET /v1/tasks`) applies that task's `delegateTo` and depth instead. Against a hostile child these limits are advisory; they stop accidental loops and runaway fan-out. If an agent must not delegate, do not configure the relay's MCP server or API access for it, and set `"delegateTo": []` as a second guard.

## Scope and operation

HTTP is the core transport because callers can use it locally or across machines and do not need to share a runtime. MCP stdio is a client interface for agents that discover tools. A2A can be added as a protocol adapter if an A2A client is required. The former A2A shaped endpoint is not part of this HTTP task API.

Tasks are held in memory and disappear on restart. At most four tasks run at once by default (`A2A_RELAY_MAX_ACTIVE`); the rest queue. Results are bounded, and completed tasks may be evicted when the task store fills. For a durable single-host deployment, add SQLite task storage. Command adapters inherit only basic process variables by default. If an adapter needs a credential already in the relay's environment, list its name in that agent's `inheritEnv` array rather than putting the secret value in the registry file. A task's timeout is one deadline that covers process startup, writing the request to a stdio adapter, execution, and reading an HTTP adapter's response. Collecting a spawned adapter's output is bounded by a short grace after it exits, so a timed-out task can be reported a few seconds after its deadline. On `SIGTERM` or `SIGINT` the relay cancels running tasks and waits briefly for adapter processes to terminate before exiting.

Resource limits can be changed with `AGENT_RELAY_TASK_RETENTION_MS` (`0`, the default, keeps finished tasks until eviction or restart), `A2A_RELAY_MAX_BODY_BYTES`, `A2A_RELAY_MAX_COMMAND_INPUT_BYTES`, `A2A_RELAY_MAX_OUTPUT_BYTES`, `A2A_RELAY_MAX_TASKS`, `A2A_RELAY_MAX_ACTIVE`, `A2A_RELAY_MAX_WAIT_MS`, `A2A_RELAY_TIMEOUT_MS` (default timeout, 15 minutes), `AGENT_RELAY_MAX_DELEGATION_DEPTH`, `AGENT_RELAY_MAX_DELEGATED_TASKS`, and `A2A_RELAY_MAX_TIMEOUT_MS` (optional hard cap; unset means no cap). Each must be a positive integer except `A2A_RELAY_MAX_TIMEOUT_MS`, `AGENT_RELAY_TASK_RETENTION_MS`, and `AGENT_RELAY_MAX_DELEGATION_DEPTH`, which accept `0` (no cap; keep until eviction; no delegation). Command input defaults to 64 KiB because it is passed as one argument and operating systems cap the combined argument and environment size. The output limit applies to the combined stdout and stderr captured from a command and to the body read from an HTTP adapter. The listener also honors `A2A_RELAY_PORT` (default `43124`) and `A2A_RELAY_HOST`; the MCP process honors `A2A_RELAY_URL`, `A2A_RELAY_HTTP_TIMEOUT_MS`, and `A2A_RELAY_AUTOSTART`. Every variable also accepts an `AGENT_RELAY_*` spelling (for example `AGENT_RELAY_TIMEOUT_MS`); the `A2A_*` names remain as aliases.

Updating: `bash scripts/update.sh` fast-forwards the checkout at `~/.local/share/agent-relay` to `main` and restarts the service only when the code changed, then verifies `/healthz`. Tasks and sessions live in memory, so the first restart is skipped while tasks are in flight; the deferral is recorded and the next run escalates — restarting unless the relay visibly reports busy tasks, so an unreadable relay (for example one running pre-update code) is activated at most one cycle late, and `--force` restarts immediately. `--no-restart` records the same deferral without attempting a restart. `bash scripts/install-service.sh --with-update-timer` additionally installs a scheduled update that runs `update.sh` every 6 hours (`AGENT_RELAY_UPDATE_INTERVAL_SECS` overrides the period); `install-service.sh --uninstall` removes both. MCP clients and the skill need no reconfiguration: the tool list refreshes on each client's next session start.

`GET /healthz` reports the effective limits, and `GET /v1/agents` reports each agent's effective `timeoutMs`.

Run `python3 -m unittest discover -s test -p 'test_*.py'` to exercise the relay and MCP interface. The tests use fake `command`, `stdio`, and `http` adapters and never call a real agent. The CLI entry points in the registry example (`claude --print`, `codex exec`, `pi --print`, `opencode run`) are starting points, not tested integrations: verify each against your installed version with `bash scripts/smoke.sh <agentId>`.
