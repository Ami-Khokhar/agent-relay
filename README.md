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

Configure that command as a local stdio MCP server in the client. It provides `prepare_feedback`, `list_agents`, `delegate`, `wait_task`, `get_task`, `list_tasks`, `cancel_task`, `run_workflow`, `get_workflow`, `list_workflows`, `cancel_workflow`, `resume_workflow`, and `get_pressure`. `A2A_RELAY_URL` overrides the HTTP address it calls. When the relay is unreachable and the URL is loopback, the MCP process starts the HTTP service detached on first use and logs it to `~/.local/state/agent-relay/relay.log`; disable that with `A2A_RELAY_AUTOSTART=0`.

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
bash scripts/install-service.sh          # launchd on macOS, systemd --user on Linux; also installs the update timer
bash scripts/install-service.sh --no-update-timer   # the service only, no scheduled update
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
| `options` | Named per-task options, each value mapped to fixed args. `command` and `stdio` agents only. See [Per-task options](#per-task-options). |
| `readOnly` | What a read-only task means for this agent: extra `args` and, on macOS, an optional `sandbox`. `command` and `stdio` agents only. See [Read-only tasks](#read-only-tasks). |

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

### Per-task options

An agent may declare named options. Each option maps its allowed values to a fixed list of arguments, so a caller chooses among what the owner allows without editing the registry:

```json
{ "id": "codex", "name": "Codex", "command": "codex", "args": ["exec", "--skip-git-repo-check", "-s", "workspace-write"],
  "options": {
    "effort": {
      "low":  ["-c", "model_reasoning_effort=low"],
      "high": ["-c", "model_reasoning_effort=high"]
    }
  } }
```

A task passes `"options": {"effort": "high"}` (the MCP `delegate` tool and `POST /v1/tasks` take the same field). The relay runs the agent's `args`, then the arguments mapped to each chosen option, then the task input. With several options, their arguments follow the order the registry declares them in, not the order the caller sent. A value may map to an empty list.

- Only registry text reaches the command line. The caller's value selects an entry; it is never passed on. An option name the agent does not declare returns `400 unknown_option`, a value it does not allow returns `400 invalid_option_value`, and a value that is not an object of strings returns `400 invalid_options`. In each case no task is created.
- `GET /v1/agents` lists each agent's options and their allowed values.
- The task record carries the chosen `options`. They are part of the `requestId` comparison: a retry with different options returns `409 idempotency_conflict`, like a different `input`.
- The relay resolves the arguments again when a queued task is dispatched, from the registry entry as it is then. A reload that changed what a value maps to changes the arguments that task runs with. A reload that removed the option or the value fails the task before it runs, with the error code submission would have given (`unknown_option` or `invalid_option_value`) at the start of its `error`.
- `command` and `stdio` agents get the arguments on their command line. The `relay.adapter/v1` request is unchanged. An `http` agent has no command line, so `options` on an `http` entry is a registry error.
- A malformed `options` entry is a registry error: the relay refuses to start with a timestamped `Configuration error: Invalid options: agent <id>: ...` line, and a reload rejects it and keeps the running registry.

### Read-only tasks

A caller can require that a task does not change files: `"readOnly": true` on `POST /v1/tasks` or the MCP `delegate` tool. The owner declares what that means for each agent with a `readOnly` profile:

```json
{ "id": "pi", "name": "Pi", "command": "pi", "args": ["--print"],
  "cwd": "/path/to/project", "allowedRoots": ["/path/to/project"],
  "readOnly": { "args": ["--exclude-tools", "edit,write"], "sandbox": true } }
```

- `args` are extra arguments. The relay adds them after the agent's `args` and any chosen options, and before the input, so no option's arguments can follow and override them.
- `inputSeparator` (`command` agents only) is an argument the relay puts after those `args` and before the input, for example `"--"`. A `command` agent gets the input as its last argument, so without a boundary an input that starts with `-` is read as an option, and with most parsers the last option wins: with `"args": ["--access=read"]`, the input `--access=write` would select write mode. The profile therefore states how the input is separated, and there are two cases. With an `inputSeparator`, any input is passed as data after it, and the agent must treat the separator as the end of its options (`--` is the convention). Without one, a read-only task whose profile has `args` and whose input starts with `-` is refused with `400 invalid_input` and nothing runs; the caller starts the input with another character. The rule applies when `sandbox` is also set, because the `args` are part of the contract. It does not apply to a `stdio` agent, whose input goes on stdin, or to a profile that has only `sandbox`, which has no argument for the input to override. A task that is not read-only is never checked.
- `sandbox: true` runs the agent under `/usr/bin/sandbox-exec` with a profile that allows everything except the blocks listed under "What the sandbox blocks" below. A tool such as `bash` cannot write under the protected directories either, but the agent can still write its own state elsewhere (session files, caches, the temp directory). On a host without `sandbox-exec` (any platform but macOS) the agent refuses read-only tasks.
- A profile must set `args`, `sandbox`, or both. An empty profile would enforce nothing, so it is a registry error. So is a `readOnly` entry on an `http` agent, an unknown field in the profile, an `inputSeparator` that is not a non-empty string, or an `inputSeparator` on a `stdio` agent.
- An agent without a profile refuses a read-only task with `400 read_only_unsupported` and nothing runs. A sandboxed profile needs somewhere to protect: without a `cwd` on the task or the agent the task is refused with `400 cwd_required`. A `readOnly` value that is not `true` or `false` (including `null`) returns `400 invalid_read_only`, so a mistyped flag never falls back to a writable run.
- The registry is checked again when a queued task is dispatched. If `POST /v1/admin/reload` changed the agent while the task waited (for example the agent became an `http` adapter, lost its profile, or no longer declares a chosen option), the task does not run. It ends `failed` with the error the submission would have returned as its code, for example `read_only_unsupported: ...`, `unknown_option: ...`, `cwd_required: ...` or `invalid_input: ...` (an input that was accepted behind an `inputSeparator` the entry no longer declares). A task that still passes runs with the option and profile `args` of the entry as it is now.
- `GET /v1/agents` shows `"readOnly": "supported"` or `"unsupported"` for each agent, and `"readOnlySandboxed": true` or `false` for a supported one. A sandboxed profile on a host without `sandbox-exec` is listed as `unsupported`.
- The task record carries `"readOnly": true`. It is part of the `requestId` comparison: a retry that changes it returns `409 idempotency_conflict`.

#### What the sandbox blocks, and what it does not

The sandboxed agent runs as the relay's OS user, so a write block alone would not be enough: it could read the relay token and ask the relay to run a writable agent in the protected tree, and that write would happen outside the sandbox. The profile therefore blocks these:

- File writes under the task's `cwd` and under the agent's `allowedRoots`, by any tool, `bash` included. A new hard link to a file in those directories is refused too.
- File writes, deletes and renames under the git metadata of the `cwd`. Before the run the relay asks git for the repository around the `cwd` (`git rev-parse --absolute-git-dir` and `--git-common-dir`) and protects both directories. For a plain checkout they sit under the `cwd` already. For a linked worktree they do not: the worktree's index, `HEAD` and reflog live in `<main checkout>/.git/worktrees/<name>`, and the refs, objects and config in `<main checkout>/.git`. Without this block a command such as `git --work-tree=/tmp/x checkout <rev> -- <path>` leaves the files alone and rewrites the real worktree's index, and `git add`, `git commit` or `git branch` change it too. A bare repository counts too: with the `cwd` inside `mirror.git/hooks`, the relay protects `mirror.git` (its `HEAD`, `refs` and `objects`) and the directories above it. Reads stay allowed, so `git log`, `git diff`, `git show` and `git status` still work. The lookup runs outside the sandbox, so it uses a git a task cannot replace and a configuration a task cannot write: see "The git the relay runs" below.
- The git lookup fails closed. The relay first looks for a sign of a repository at or above the real path of the `cwd`: a `.git` file or directory, or a directory that looks like a git directory the way git checks it, with a `HEAD` file and `objects` and `refs` directories (a bare repository has no `.git`). If there is none, the `cwd` is outside any repository: git is not run, and the task runs with the `cwd` protected. If there is one, git must answer. When git is missing or cannot start, takes longer than five seconds or the time the task has left, exits non-zero (for example because it does not trust the repository's owner), or prints something that is not a directory, the task does not run with less protection. A directory that only looks like a git directory, and that git does not accept, is refused the same way. The task ends `failed` at dispatch with an error that starts `read_only_git_unknown:` and names the place where git metadata was expected and git's message. It is a failed task and not a `400`, because the lookup runs when the sandbox is built, after the task was accepted, so it describes the repository at the moment the agent would start. Fix git for that directory, or submit without `readOnly`.
- Renaming or deleting any directory above a protected tree, the `cwd`, each of the `allowedRoots` and each git directory. Without this rule an agent with the `cwd` `/tmp/review/tree` could rename `/tmp/review` and write through the new path, which no rule on `/tmp/review/tree` matches. Writes beside a protected tree, inside such a directory, stay allowed.
- Reading, changing or deleting the relay's token file (the `AGENT_RELAY_TOKEN_FILE` path, default `~/.config/agent-relay/token`), and renaming it or any directory above it, so the file cannot be moved to a name the read block does not cover. The token's ancestors follow the same rule as the protected trees above.
- Connecting to the relay's own TCP port, on `127.0.0.1`, `::1` or any other address, so the agent cannot call the relay API at all. This also means a sandboxed agent cannot use the relay's MCP tools to delegate further.
- The environment variables `AGENT_RELAY_TOKEN` and `A2A_RELAY_TOKEN`: a sandboxed task never receives them, even when `inheritEnv` or `env` lists them for the agent's ordinary runs.

It does not block these:

- A hard link that already exists outside the protected directories and points to a file inside them. The sandbox matches paths, not files, so a write through that other name changes the protected file.
- Writes made for the agent by another local service it can reach, for example the Docker socket (`docker run -v` of the protected tree), a local daemon on a Unix socket or another TCP port, or a proxy in front of the relay on a different port. A network block on one port does not stop these.
- Reads. The agent can read any file its user can read, except the relay token file.
- Other network access. The agent can reach its model API, the internet and other ports on localhost.
- Writes anywhere outside the protected directories, including through a symlink in the `cwd` that points outside. That includes planting a file that another program trusts later: an executable in a directory on your `PATH`, `~/.gitconfig`, a shell start-up file. The relay's own git lookup and revision check ignore all of these (see below), but your shell, your editor and other agents do not.
- Reading the relay's environment. If the relay got its token from `AGENT_RELAY_TOKEN` instead of the token file, a sandboxed agent can still read that process's environment with a `sysctl` call (`kern.procargs2`). It cannot use the token against the relay, because the port is blocked, but keep the token in the file (the default) so it is in no process environment.
- The git metadata of any repository other than the one around the `cwd`, for example a superproject above a submodule, or a repository the agent reaches with `git -C` or `--git-dir`. List its directory in `allowedRoots` to protect it.

The sandbox is therefore not general confinement. The git metadata of the `cwd` is protected without any setting, a worktree's included, so a read-only review in a worktree cannot change its index, refs or objects, and the directories above every protected tree cannot be moved aside. `sandbox-exec` is deprecated by Apple but ships with macOS, and macOS does not nest sandboxes: an agent that applies its own sandbox (Codex does) fails under `"sandbox": true`, so give such an agent `args` that select its own read-only mode instead. If the relay itself runs inside a sandbox, sandboxed tasks fail the same way.

#### The git the relay runs

The git lookup above, and the `git log` that reads the relay's own `revision` at startup, run outside the sandbox as the relay's user, which is also the user a read-only task runs as. A task may write anywhere outside the protected directories, so it could plant an executable named `git` in a writable directory on the relay's `PATH`, or a `~/.gitconfig`, and the next read-only task's lookup (or the next relay start) would run or obey it with no sandbox. The relay closes that route:

- **A trusted git, by absolute path.** The relay never searches `PATH` for git. It runs `/usr/bin/git` (on macOS a SIP-protected shim) or, where that is missing, `/bin/git`. To use another git, set `AGENT_RELAY_GIT` in the relay's own environment (for example in the service definition): the value must be an absolute path to an existing executable file, or the relay exits at startup with an error. The relay resolves it once, at startup, and a task cannot change the environment of the running relay. Name a file that agents cannot write: a git under your home directory, or in a Homebrew prefix that your user owns, can be replaced by a task. On macOS the `/usr/bin/git` shim finds the real git through `xcrun`, which keeps a lookup cache (`xcrun_db`) in your user's temporary directory, and a task can write there. Whether `xcrun` would follow a changed cache is not tested here. To skip the shim, set `AGENT_RELAY_GIT` to the real binary that `xcrun --find git` prints, which is root-owned in a standard Xcode or Command Line Tools install. When the relay has no usable git, a read-only task in a repository is refused (`read_only_git_unknown`, naming `AGENT_RELAY_GIT`), a read-only task outside any repository still runs, and `revision` reads `unknown`.
- **A minimal environment.** The relay builds git's environment from nothing: `PATH=/usr/bin:/bin`, `HOME` as the relay's own, `GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_GLOBAL=/dev/null`. No other variable passes, so no `GIT_*` variable, `DEVELOPER_DIR` (which would redirect the `/usr/bin/git` shim), `XDG_CONFIG_HOME` or loader variable of the relay's can change which git runs or what it reads. `GIT_CONFIG_GLOBAL` needs git 2.32 or newer; an older git would still read `~/.gitconfig`.
- **The global git configuration is not read.** A read-only task can write `~/.gitconfig`, so the relay's git ignores it, the XDG config file and the system config. The cost: a repository that git accepts only because `safe.directory` is set in your global config is refused (the lookup fails closed, as above). Fix the ownership of the directory, or submit without `readOnly`. The repository's own `.git/config` is still read, and it is inside the protected tree.

Example registry entries for Codex and Pi (flags checked against codex-cli 0.162.1 and pi 1.0.4):

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

Codex rejects a repeated `--sandbox`/`-s` (`the argument '--sandbox <SANDBOX_MODE>' cannot be used multiple times`), so a Codex entry with a read-only profile cannot also have `-s workspace-write` in its base `args`. The example leaves `-s` out of `args` (`codex exec` then runs read-only) and offers it as the `access` option: a task that needs to edit files passes `"options": {"access": "write"}`. A task that asks for both `readOnly` and `access: write` fails with Codex's error instead of running writable. Codex enforces `readOnly` through its own sandbox (`--sandbox read-only`), so the entry does not set `"sandbox": true`.

## Report a relay problem

Feedback stays quiet unless agent-relay itself blocks a concrete action. First use a concrete
workaround and resume work; only the user-facing orchestrator may offer a report at a natural
checkpoint, at most once per work session. Normal errors, warnings, retries, worker-code failures,
and provider/configuration issues alone are not triggers. Decline, defer, or silence ends the flow.

If wanted, MCP `prepare_feedback` creates a small local draft and a prefilled canonical GitHub
issue page. It requires both the blocked action and workaround actually used. Missing either yields
no draft. The tool does not contact GitHub or submit anything. A user must open the page, review
the public contents, and click Submit in their own GitHub session.

Use only the bounded error category, operation, OS family, version, blocked action, and workaround.
The version must be SemVer, a 7-40 character hexadecimal revision, or `unknown`. The draft omits
task prompts, outputs, paths, environment values, and IDs by default. Credential-shaped values
and control or bidirectional characters in free text are refused, but that check cannot guarantee
arbitrary prose is private. A report does not authorize repairs or updates.

## Submit and inspect work

```bash
AUTH="authorization: Bearer $(cat ~/.config/agent-relay/token)"
curl -sS -H "$AUTH" http://127.0.0.1:43124/v1/agents
curl -sS -H "$AUTH" http://127.0.0.1:43124/v1/tasks \
  -H 'content-type: application/json' \
  -d '{"agentId":"codex","requestId":"review-2026-09-23-1","input":"Inspect this project and report the main risks","cwd":"/path/to/git/project"}'
curl -sS -H "$AUTH" 'http://127.0.0.1:43124/v1/tasks?sessionId=review-2026-09-23-1'
curl -sS -H "$AUTH" 'http://127.0.0.1:43124/v1/tasks/TASK_ID?waitMs=600000'
curl -sS -H "$AUTH" 'http://127.0.0.1:43124/v1/tasks/TASK_ID?includeInput=true'
curl -sS -H "$AUTH" -X DELETE http://127.0.0.1:43124/v1/tasks/TASK_ID
```

Add `"options": {"<name>": "<value>"}` to choose among the options the agent declares (see [Per-task options](#per-task-options)), and `"readOnly": true` to require a run that cannot change files (see [Read-only tasks](#read-only-tasks)). Submission returns `202` with a task ID and session ID. Poll the task URL for `queued`, `running`, `completed`, `failed`, `timed_out`, or `cancelled`. `GET /v1/tasks/TASK_ID?waitMs=<ms>` long-polls until the task is terminal or the wait elapses. A `waitMs` that is not an integer from 1 to `A2A_RELAY_MAX_WAIT_MS` (default 600000) returns `400 invalid_wait`, and its message names that range. If the wait elapses while the task is still `queued` or `running`, the `200` response carries `waitExpired: true`, `waitedMs` (the wait that elapsed), and a `hint` to wait again; a terminal task carries none of these. `POST /v1/tasks` also accepts `waitMs` and checks it the same way before it creates the task, so a refused wait leaves no task behind. The MCP `wait_task` tool clamps a `maxWaitMs` above the relay's limit to that limit (read from `/healthz` `limits.maxWaitMs`) and adds `waitMs` and `waitClamped: true` to the result; the MCP `delegate` tool does the same for `waitMs`, retrying once with the limit. Claude Code moves an MCP call that runs longer than 120 s to the background, so with it use a `maxWaitMs` of about 110000 or less and call `wait_task` again while `waitExpired` is true. `GET /v1/tasks?sessionId=...&status=...&limit=...` lists stored tasks (most recent first). Supply a unique `requestId` and reuse it when retrying a submission; the relay returns the original task instead of starting duplicate work. Reusing it with different task fields returns `409`. Without a request ID, callers must not automatically retry a submission whose response was lost. The session ID currently groups work for the caller; generic command adapters do not continue native agent conversations.

Task objects (the submit, get, wait, list and cancel responses, and the MCP tools behind them) leave out `input` and carry `inputChars`, its length in characters, so a poll does not echo the prompt the caller already holds. `GET /v1/tasks/TASK_ID?includeInput=true` (MCP `get_task` with `includeInput: true`) returns it. This keeps replies small and is not access control: any token holder can still read every stored input. The request a `stdio` or `http` adapter receives still carries `input`.

## Run a workflow

A workflow is one JSON document: named steps, each bound to a registered agent, wired together with `needs`. The relay starts a step only once every step it needs has completed and threads each output into the next prompt, so a pipeline, a fan-out, or a review loop is one submission instead of a state machine in the caller.

```bash
AUTH="authorization: Bearer $(cat ~/.config/agent-relay/token)"
curl -sS -H "$AUTH" http://127.0.0.1:43124/v1/workflows \
  -H 'content-type: application/json' \
  -d '{"name":"review-loop","steps":[
        {"id":"write","agentId":"claude","input":"Fix the failing test"},
        {"id":"review","agentId":"codex","needs":["write"],"input":"Review this change and report findings:\n\n{{write.output}}"},
        {"id":"prove","agentId":"claude","needs":["review"],"input":"Address the review of:\n\n{{write.output}}"}]}'
curl -sS -H "$AUTH" 'http://127.0.0.1:43124/v1/workflows/WORKFLOW_ID?waitMs=600000'
curl -sS -H "$AUTH" 'http://127.0.0.1:43124/v1/workflows/WORKFLOW_ID?view=summary'
curl -sS -H "$AUTH" 'http://127.0.0.1:43124/v1/workflows/WORKFLOW_ID?step=STEP_ID'
curl -sS -H "$AUTH" 'http://127.0.0.1:43124/v1/workflows/events?sessionId=SESSION_ID&after=0&waitMs=600000'
curl -sS -H "$AUTH" 'http://127.0.0.1:43124/v1/workflows?status=completed'
curl -sS -H "$AUTH" -X DELETE http://127.0.0.1:43124/v1/workflows/WORKFLOW_ID
curl -sS -H "$AUTH" -X POST http://127.0.0.1:43124/v1/workflows/WORKFLOW_ID/resume
```

Step fields:

| Field | Notes |
| --- | --- |
| `id` | Unique in the workflow. Matches `[A-Za-z0-9_.~-]`, at most 128 characters. |
| `agentId` | A registered agent. |
| `input` | Prompt for that agent. May contain `{{stepId.output}}`, and `{{item}}` and `{{index}}` on a `foreach` step. A reference must name a step in `needs`, directly or through another step. |
| `needs` | Step ids that must complete first. The graph must be acyclic. |
| `foreach` | Fans the step out over an array of strings. The step completes when every item does, and `{{stepId.output}}` then reads as a JSON array of the item outputs. |
| `retries` | Extra attempts after the task fails or times out, `0` to `5`. |
| `repeat` | `{"max": 1..10, "untilOutputMatches": "regex"}` re-runs the step until its output matches. Spending `max` without matching fails the step with `repeat_limit_reached`. |
| `failIfOutputMatches` | A Python regular expression. When a completed task's output matches it, the step fails with `output_matched_fail_pattern` and its dependents are skipped — an agent that reports `BLOCKED: ...` while exiting 0 cannot feed a later step. A malformed pattern is refused at submit time. |
| `timeoutMs` / `cwd` | Per step, checked exactly like a task submission, including `allowedRoots`. |
| `options` | `{"<name>": "<value>"}` for that agent, exactly as for a task submission. An option the agent does not declare fails the step with `unknown_option` or `invalid_option_value`. |
| `readOnly` | `true` runs that step read-only, exactly as for a task submission. An agent with no read-only profile fails the step with `read_only_unsupported`. |

Submission returns `202` with a run ID and a step list; `GET /v1/workflows/:id?waitMs=<ms>` long-polls until the run is terminal or the wait elapses, and a wait that expires reports `waitExpired: true` exactly as a task wait does. `GET /v1/workflows` lists runs. Every step carries `startedAt` and `finishedAt`, both `null` until the step starts and finishes — a step that never ran keeps them `null`. A full run view also carries `attemptTimes`, one `{startedAt, finishedAt}` window per attempt, so a `retries` or `repeat` step reports each try separately instead of only the run's own window. A step whose dependency did not succeed is `skipped`, and the run finishes as `failed` with `error` naming the first bad step and, when that step failed, the short cause the agent printed last (the last non-empty lines of its stderr and output, with `agent_quota: ` in front when they name a usage limit or rate limit); the failing task's full stderr stays on the step as `errorDetail`, in a single-run view only. When an orchestrator only needs to know which step finished, `?view=summary` leaves out the `spec` and every full output: each step then carries `outputChars` and `firstLine` (with its identity, status, attempts and time windows) instead, so a long-poll that ends does not re-deliver every transcript. `?step=<stepId>` fetches that one step alone with its full output and `errorDetail`, and no `spec`; both selectors combine with `?waitMs=`. An unknown `view` is `400 invalid_view`, a malformed `step` id is `400 invalid_step`, and a step the run does not have is `404 unknown_step`. `DELETE` cancels the run and every task it has started. `requestId` gives a submission the same idempotency it has for tasks. Every child task shares the run's `sessionId` unless the document sets one.

To react to whichever run in a session finishes first, without polling each run, read the session's change feed: `GET /v1/workflows/events?sessionId=<id>&after=<seq>&waitMs=<ms>` long-polls for the next step or run status change and returns `{"events": [...]}`, oldest first. Each event carries `workflowId`, `stepId` (`null` for a run-level change), `oldStatus`, `newStatus`, and a `seq` that rises across the relay; pass the last `seq` you saw as `after` to continue the feed, and a wait that ends with no change returns an empty list. A `waitMs` outside `1..A2A_RELAY_MAX_WAIT_MS` is refused exactly as on `GET /v1/workflows/:id`, and the MCP `wait_workflow_events` tool clamps an overlong `maxWaitMs` as the other wait tools do. The feed lives in relay memory and keeps at most 4096 events in total, so the oldest events fall off; an `after` older than that still works and simply skips what is gone.

Runs are journaled to `<state dir>/workflows/<id>.jsonl` (`AGENT_RELAY_STATE_DIR`, default `~/.local/state/agent-relay`), so a step that completed keeps its output and its time window across a relay restart. `POST /v1/workflows/:id/resume` continues such a run, re-running only the steps that never finished, and `GET /v1/workflows/:id` for a run that is no longer active returns `404` with a message pointing at resume. A step whose child task was evicted or expired before the run could read it fails with `task_evicted_from_the_relay_store`, so set `AGENT_RELAY_TASK_RETENTION_MS` with care if you rely on long runs: the default keeps finished tasks until the store fills or the relay restarts.

## Device pressure

A long multi-agent run is the most expensive thing the relay starts, so it can read the host's
pressure before and during a run.

```bash
AUTH="authorization: Bearer $(cat ~/.config/agent-relay/token)"
curl -sS -H "$AUTH" http://127.0.0.1:43124/v1/pressure
curl -sS -H "$AUTH" -X POST http://127.0.0.1:43124/v1/pressure/kill
```

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

The verdict is `critical` when the kernel reports critical memory pressure, less than 4% of
memory is free, swap is 90% used or more, or the CPU is throttled below 40% of its speed; `warn`
uses the softer version of the same readings (kernel warning, 12% free, 70% swap, 75%
throttled); otherwise `ok`. `reasons` names the reading that decided it, and each signal is
`null` when the host cannot report it. The numbers are read from `vm.memory_pressure`,
`kern.memorystatus_level`, `vm.swapusage` and `pmset -g therm`, so a verdict always names a
number rather than an opaque flag. `admitBelow` is the owner's admission line: the verdict at or
above which a new workflow is refused, `AGENT_RELAY_ADMIT_BELOW` read from the relay's own
environment, `critical` unless the owner sets `warn` or `ok`.

Both probes run outside the sandbox as the relay's user, so the relay runs them by absolute path
and never through `PATH`: `/usr/sbin/sysctl` and `/usr/bin/pmset`, or the first trusted path that
exists. A task may write anywhere outside the protected directories, and a `sysctl` it planted on
`PATH` must not become the program the relay runs every few seconds. `AGENT_RELAY_SYSCTL` and
`AGENT_RELAY_PMSET` in the relay's own environment name another absolute executable, which is how
the tests drive each reading without a busy machine.

- **Before a run.** `POST /v1/workflows` returns `503 device_pressure` while the verdict is at
  or above `AGENT_RELAY_ADMIT_BELOW` (`critical` by default, `warn` or `ok` if the owner sets
  it), naming the reading that decided it and carrying the whole reading, so nothing starts on a
  machine that cannot take it. With the default a `warn` verdict still runs work;
  `AGENT_RELAY_ADMIT_BELOW=warn` refuses at `warn` too, for a host whose kernel warns well
  before it turns critical, and `=ok` refuses every verdict, saying which setting refused it.
- **During a run.** With `AGENT_RELAY_PRESSURE_GUARD` on (the default) the relay samples every
  `AGENT_RELAY_PRESSURE_INTERVAL_MS` (default 5000) but only while at least one run is active, so an
  idle relay never shells out. It stops every active run only once the verdict has been `critical`
  for `AGENT_RELAY_PRESSURE_HOLD_MS` milliseconds (default 30000) and has been read at least twice,
  so one critical reading between normal ones costs a running step nothing while a state that
  lasts does. `AGENT_RELAY_PRESSURE_HOLD_MS=0` keeps stop-at-once: the first critical reading ends
  the run.
- **The kill button.** `POST /v1/pressure/kill` is the same stop on demand, whether or not the
  verdict is critical, and reports the reading it saw.

A stopped run is `cancelled` with `device_pressure:` at the start of its `error`, naming how long
pressure stayed critical and the readings that decided the stop, and
`POST /v1/workflows/:id/resume` continues it from the last step that finished: only the step that
was running is repeated. The checkpoint is the step, so a killed adapter's own partial work is lost
— the relay cannot checkpoint inside another agent's process.

The probes are macOS-only. On another host every signal is null, the verdict is `ok`, and the
guard never stops a run, so `ok` there means "not measured" rather than "healthy".

`temperatureC` is null unless the owner allowlists an unprivileged reader: `powermetrics` needs
root and macOS exposes no unprivileged sensor, but `macmon` reads the sensors through IOKit
without root. Name it in the relay's own environment as a command line whose first word is an
absolute path to an executable, and the relay runs it on each sample and takes the Celsius
`temp.cpu_temp_avg` from its `macmon pipe` JSON line. The reader must answer within the 2 s probe
timeout, so pass one-sample flags:

```bash
AGENT_RELAY_TEMP_READER="/opt/homebrew/bin/macmon pipe -s 1 -i 200"
```

With no reader, or one that does not answer, `temperatureC` is null and `temperatureReason`
names the cause. `AGENT_RELAY_TEMP_WARN_C` (for example `75`) adds a `warn` reason when the
temperature reaches it; like every other warn reason, it does not stop work. `pmset -g therm`
reports a throttle only after macOS records one, so a null `speedLimitPercent` means no throttle
was recorded, not that the reading failed.

## Data access and retention

- **What is stored.** Each task keeps its full `input` (the prompt), `output`, `error`, `cwd`, `sessionId`, and `requestId` in the relay's memory. Nothing about a task is written to disk and the relay does not log task content. The operational log (`relay.log`, where launches and installed services send the relay's stdout and stderr) holds one line per event: each line starts with a UTC ISO 8601 timestamp (`2026-10-26T16:33:00+00:00`) and lines about work name the ids they concern — the shutdown line lists the task ids it cancelled, the device-pressure stop lists the workflow run ids it stopped — so a restart or a forced stop can be read back from the log alone. The one exception is a workflow run: it journals the submitted document and every completed step's output and time window to `<state dir>/workflows/<id>.jsonl` (`AGENT_RELAY_STATE_DIR`, default `~/.local/state/agent-relay`), so a resumed run can skip the steps that finished and still report when they ran. That file holds prompts and results, so treat it as task content and remove it when you no longer need to resume the run.
- **Who can read it.** Anyone holding the API token, through `GET /v1/tasks` and `GET /v1/tasks/:id`, which returns the `input` only with `?includeInput=true` (see [Local security model](#local-security-model)); a `sessionId` is a filter, not access control. The adapter request carries only its own task, but an agent the relay started runs as your OS user, can read the token file, and so can read every stored task.
- **When it is removed.** On relay restart; when the store reaches `A2A_RELAY_MAX_TASKS` (the oldest finished task is evicted per new submission); or, when `AGENT_RELAY_TASK_RETENTION_MS` is set, once that period has passed since the task finished. Expiry is checked on every `/v1/` request and in the background at least once a minute (or once per period, if shorter). Removing a task also releases its `requestId`, so a retry after the period starts new work: keep the period longer than your clients' retry window. The defaults (1000 tasks, 1 MiB bodies, 256 KiB output, no retention period) can hold a lot of sensitive text; if prompts or results are sensitive, set a short retention period. An installed service does not inherit your shell environment, so add the variable to its unit (`Environment=AGENT_RELAY_TASK_RETENTION_MS=...`) or plist (`EnvironmentVariables`) and restart it.
- **Environment filtering is not a sandbox.** `command` and `stdio` adapters receive only basic environment variables plus `inheritEnv`/`env`, but they run as the relay's OS user, with its permissions. An agent can read any file that user can (home directory, SSH keys, other projects). `cwd` and `allowedRoots` choose where an agent starts; they do not confine it. For isolation, use the agent's own sandbox (for example `codex -s workspace-write`), a separate OS user, or a container. A `readOnly` profile with `"sandbox": true` (macOS) blocks writes under the task's `cwd`, the agent's `allowedRoots` and the git metadata of the `cwd` (a worktree's too), reading the relay token and connecting to the relay port. It does not block reads, other network access, a hard link that already exists outside the tree, or writes made for the agent by other local services such as a Docker socket (see [What the sandbox blocks, and what it does not](#what-the-sandbox-blocks-and-what-it-does-not)).

## Local security model

The relay is a **single-user local service**. Its trust boundary is one bearer token:

- Every `/v1/` route (agents, task submission, reads, listing, cancellation, reload) requires `Authorization: Bearer <token>`; otherwise it returns `401`. `/healthz` stays open and returns no task data. The token comes from `AGENT_RELAY_TOKEN`, else the file at `AGENT_RELAY_TOKEN_FILE` (default `~/.config/agent-relay/token`). If you set `AGENT_RELAY_TOKEN`, the relay writes no token file, so give every client the same value: the MCP server's environment and, for agents that delegate, `"inheritEnv": ["AGENT_RELAY_TOKEN"]`. The relay creates the file with mode `0600` on first start and refuses a symlinked file or, on POSIX, one other users can read. The token is read once at startup: after rotating it, restart the relay. `scripts/smoke.sh` accepts only the letters, digits, `-` and `_` the relay generates. The MCP client refuses (`insecure_relay_url`) to send the token over `http://` to a non-loopback relay. The token is never returned by the API or logged. `curl -H` puts it in the process list, so on a shared machine pass it with `curl -K` instead (as `scripts/smoke.sh` does).
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

Tasks are held in memory and disappear on restart, so a workflow run's journal is the only thing that outlives a restart and it exists solely so a run can be resumed. At most four tasks run at once by default (`A2A_RELAY_MAX_ACTIVE`); the rest queue. Results are bounded, and completed tasks may be evicted when the task store fills. For a durable single-host deployment, add SQLite task storage. Command adapters inherit only basic process variables by default. If an adapter needs a credential already in the relay's environment, list its name in that agent's `inheritEnv` array rather than putting the secret value in the registry file. A task's timeout is one deadline that covers process startup, writing the request to a stdio adapter, execution, and reading an HTTP adapter's response. Collecting a spawned adapter's output is bounded by a short grace after it exits, so a timed-out task can be reported a few seconds after its deadline. On `SIGTERM` or `SIGINT` the relay cancels running tasks and waits briefly for adapter processes to terminate before exiting.

Resource limits can be changed with `AGENT_RELAY_TASK_RETENTION_MS` (`0`, the default, keeps finished tasks until eviction or restart), `A2A_RELAY_MAX_BODY_BYTES`, `A2A_RELAY_MAX_COMMAND_INPUT_BYTES`, `A2A_RELAY_MAX_OUTPUT_BYTES`, `A2A_RELAY_MAX_TASKS`, `A2A_RELAY_MAX_ACTIVE`, `A2A_RELAY_MAX_WAIT_MS`, `A2A_RELAY_MAX_STEPS` (workflow steps per run, default 64), `A2A_RELAY_MAX_WORKFLOWS` (stored runs, default 200), `A2A_RELAY_PRESSURE_INTERVAL_MS` (how often an active run's host pressure is re-read, default 5000), `A2A_RELAY_PRESSURE_HOLD_MS` (how long pressure must stay critical before a running step is stopped, default 30000), `AGENT_RELAY_PRESSURE_GUARD` (`1`, the default; `0` disables the pressure stop), `AGENT_RELAY_ADMIT_BELOW` (the verdict at or above which a new workflow is refused; `critical`, the default, `warn`, or `ok`), `AGENT_RELAY_TEMP_READER` (unset by default; otherwise a command line whose first word is an absolute path to the owner's allowlisted unprivileged temperature reader, for example `macmon`), `AGENT_RELAY_TEMP_WARN_C` (unset by default; otherwise the Celsius degrees at which the reading adds a `warn` reason), `A2A_RELAY_MAX_FOREACH` (items per fan-out, default 32), `A2A_RELAY_STATE_DIR` (a path, default `~/.local/state/agent-relay`, holding the workflow journals), `A2A_RELAY_TIMEOUT_MS` (default timeout, 15 minutes), `AGENT_RELAY_MAX_DELEGATION_DEPTH`, `AGENT_RELAY_MAX_DELEGATED_TASKS`, and `A2A_RELAY_MAX_TIMEOUT_MS` (optional hard cap; unset means no cap). Each must be a positive integer except `A2A_RELAY_MAX_TIMEOUT_MS`, `AGENT_RELAY_TASK_RETENTION_MS`, `A2A_RELAY_PRESSURE_HOLD_MS` (default 30000; `0` stops on the first critical reading), and `AGENT_RELAY_MAX_DELEGATION_DEPTH`, which accept `0` (no cap; keep until eviction; stop at once; no delegation), and `A2A_RELAY_STATE_DIR`, which is a path, and `AGENT_RELAY_PRESSURE_GUARD`, which is `0` or `1`, and `AGENT_RELAY_TEMP_READER`, which is a command line, and `AGENT_RELAY_TEMP_WARN_C`, which is a number of degrees (a value that is not a number is ignored), and `AGENT_RELAY_ADMIT_BELOW`, which is `ok`, `warn` or `critical`. A value for `A2A_RELAY_PRESSURE_HOLD_MS` that is not a non-negative integer, or for `AGENT_RELAY_ADMIT_BELOW` that is not `ok`, `warn` or `critical`, stops the relay at startup, naming the variable. Command input defaults to 64 KiB because it is passed as one argument and operating systems cap the combined argument and environment size. The output limit applies to the combined stdout and stderr captured from a command and to the body read from an HTTP adapter. The listener also honors `A2A_RELAY_PORT` (default `43124`) and `A2A_RELAY_HOST`; the MCP process honors `A2A_RELAY_URL`, `A2A_RELAY_HTTP_TIMEOUT_MS`, and `A2A_RELAY_AUTOSTART`. Every variable also accepts an `AGENT_RELAY_*` spelling (for example `AGENT_RELAY_TIMEOUT_MS`); the `A2A_*` names remain as aliases.

Updating: `bash scripts/update.sh` fast-forwards the checkout at `~/.local/share/agent-relay` to `main` and restarts the service only when the code changed, then verifies `/healthz`. Tasks and sessions live in memory, so the first restart is skipped while tasks are in flight; the deferral is recorded and the next run escalates — restarting unless the relay visibly reports busy tasks, so an unreadable relay (for example one running pre-update code) is activated at most one cycle late, and `--force` restarts immediately. `--no-restart` records the same deferral without attempting a restart. `bash scripts/install-service.sh` also installs a scheduled update that runs `update.sh` every 6 hours (`AGENT_RELAY_UPDATE_INTERVAL_SECS` overrides the period), so an installed relay cannot fall behind `main` unnoticed. It does so only when the install directory is a git clone, because `update.sh` cannot update anything else; otherwise it prints a warning and installs the service alone. Pass `--no-update-timer` to opt out and run `update.sh` yourself; it also removes an update timer that an earlier install left behind and leaves the relay service alone. `--with-update-timer` is still accepted and does nothing, because the timer is now the default. An install made before this default has no timer until you rerun the installer. `install-service.sh --uninstall` removes both. MCP clients and the skill need no reconfiguration: the tool list refreshes on each client's next session start.

`GET /healthz` reports the effective limits, and `GET /v1/agents` reports each agent's effective `timeoutMs`. Both also report the deployed `revision`, and so does the MCP `list_agents` result (it returns the `GET /v1/agents` payload):

```json
{"revision": {"commit": "131c0c0a3be70175e4bedaf4ef6074d0527f9cb9", "short": "131c0c0", "date": "2026-10-02T19:10:52Z"}}
```

It names the git commit that the relay process started from and that commit's date (ISO 8601, in the committer's time zone). The relay reads it once at startup, with its trusted git and minimal environment (see "The git the relay runs"), so a pulled update shows only after the restart: a `date` that is weeks old means the running relay is stale. Each field is `"unknown"` when the relay does not run from a git checkout or git cannot answer; that never stops the relay from starting. A relay older than this field omits `revision`.

Run `python3 -m unittest discover -s test -p 'test_*.py'` to exercise the relay and MCP interface. The tests use fake `command`, `stdio`, and `http` adapters and never call a real agent. The CLI entry points in the registry example (`claude --print`, `codex exec`, `pi --print`, `opencode run`) are starting points, not tested integrations: verify each against your installed version with `bash scripts/smoke.sh <agentId>`.
