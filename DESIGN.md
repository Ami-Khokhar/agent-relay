# Relay design decision

## Use case

A coding agent can delegate a **new** task to another registered agent, inspect its progress, get its result, or cancel it. The first release does not resume an existing interactive session. Every adapter declares its actual capabilities; a correlation `sessionId` is not a promise of native session continuity.

## Transport and protocol choice

| Method | Best fit | Result of the experiment |
| --- | --- | --- |
| HTTP task API | Shared relay on one host or across machines; asynchronous jobs | Chosen as the core. Submission returns a task ID; clients poll or cancel. |
| MCP over stdio | Coding agents that can discover and call tools | Chosen as a thin interface over the HTTP API. It uses the six task tools, the five workflow tools, and `get_pressure`. |
| A2A over HTTP | Interoperability with clients that require the A2A standard | Keep as a future edge adapter. A prototype showed the Agent Card and JSON-RPC surface require their own versioning and task semantics. |
| Direct CLI / stdio | One orchestrator and one local child agent | Simplest for a single call, but gives no shared task registry or remote access. |
| Unix socket | Local transport with no TCP port | Could replace the HTTP listener locally; it does not change the task API or adapter model. |

HTTP is a transport, while MCP and A2A are protocols. They can coexist around one task service. WebSockets are useful for live, two-way steering; the first release needs task submission, polling, and cancellation. Server-Sent Events can later provide output events while preserving polling as the baseline.

## Shape

```text
Codex / Claude Code / Pi / OpenCode / other clients
              | MCP tools or HTTP
              v
      HTTP task service
      registry + queue + task state
              |
      command, stdio, or HTTP adapter
              v
      target coding agent
```

The generic `relay.adapter/v1` boundary supports a process over stdio or a hosted HTTP endpoint. Both receive the same versioned JSON task and return the same versioned completed or failed result. Harness-specific flags, APIs, credentials, and output conversion stay in the external wrapper. A hosted adapter may run on its own localhost port while clients continue using the relay's single stable port. The legacy command adapter passes caller input as one final argument without a shell and has a smaller limit to stay below operating-system argument limits.

A registry entry may also declare named per-task options, each value mapped to a fixed list of arguments (`"options": {"effort": {"high": ["-c", "model_reasoning_effort=high"]}}`). A task selects values by name (`"options": {"effort": "high"}`). The relay inserts the mapped arguments after the agent's configured arguments and before the task input. The caller's text only picks a registry entry, so no caller-supplied string reaches the command line and a caller cannot inject flags. Unknown names and values are refused before a task exists. The chosen options belong to the task record and to the `requestId` comparison. The relay checks them again when the task is dispatched, against the registry entry as it is then: a reload that removed an option or a value fails the queued task with the error submission would have given, and a reload that changed what a value maps to changes the arguments the task runs with. A queued task never runs with the arguments of an entry that is gone. Options apply to `command` and `stdio` agents; the `relay.adapter/v1` request does not carry them, because the adapter already receives them as arguments.

A task may also set `"readOnly": true`. The relay either enforces that or refuses the task before it exists. Enforcement comes from the owner's registry: a `readOnly` profile of extra arguments (for example `pi --exclude-tools edit,write`) and, on macOS, `"sandbox": true`, which runs the agent under `sandbox-exec` with a profile that allows everything except the blocks below. Arguments alone depend on the agent honouring them, since a tool such as `bash` can still write; the sandbox is what stops that, and the agent can still write its own state elsewhere. A command agent receives the task input as its last argument, after the profile's arguments, so an input such as `--access=write` could override an args-only profile (`--access=read`). The profile therefore says how the input is separated: with an `inputSeparator` (for example `"--"`) the relay puts it before the input and any input is data; without one, an input that starts with `-` is refused with `invalid_input` for a profile that has `args`. The rule holds beside a sandbox too, runs again at dispatch against the current entry, and does not apply to a stdio agent (the input is on stdin) or to a sandbox-only profile (no argument to override). An agent with no profile refuses (`read_only_unsupported`), as does a sandboxed profile on a host without `sandbox-exec`, and `list_agents` reports `readOnly: supported | unsupported` so a caller can choose an agent first. The same checks run again at dispatch against the current entry, because a reload can turn a queued read-only task's agent into an `http` adapter or strip its profile; the task then fails with the same error code and never runs without the enforcement it asked for. Paths and the relay port reach `sandbox-exec` as `-D` parameters, never as profile text, so a value cannot alter the profile. The flag is part of the task record and the `requestId` comparison, and a non-boolean value is refused rather than treated as false.

The sandboxed agent runs as the relay's user, so blocking writes alone leaves a way round: read the relay token, call the relay, and have a writable agent change the tree from outside the sandbox. The profile closes that route with four blocks. It denies writes under the task's `cwd`, the agent's `allowedRoots` and the git metadata of the `cwd` (a new hard link to a file there is refused as well). The relay finds the git metadata with `git rev-parse --absolute-git-dir` and `--git-common-dir` before the run, because a linked worktree keeps its index under the main checkout's `.git/worktrees/<name>` and its refs and objects in the main checkout's `.git`, outside the `cwd`. Without that block `git --work-tree=/tmp/x checkout <rev> -- <path>` rewrites the worktree's index. Reads of the metadata stay allowed, so `git log`, `git diff` and `git show` work. The lookup fails closed: it runs only when a sign of a repository exists at or above the real `cwd` (a `.git` entry, or a directory with a `HEAD` file and `objects` and `refs` directories, which is how a bare repository looks), and any git failure (missing, too slow for the task's remaining time, non-zero exit, unusable output) fails the task at dispatch with `read_only_git_unknown` instead of running it with less protection. A `cwd` with no such sign above it is confirmed to be outside a repository and needs no git. The lookup runs outside the sandbox, as the user a read-only task runs as, so it must not run anything the task could have planted: it runs the relay's trusted git by absolute path (`/usr/bin/git`, or the owner's `AGENT_RELAY_GIT`, resolved once at startup, never through `PATH`) in an environment built from nothing (`PATH=/usr/bin:/bin`, the relay's `HOME`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`), and the relay's startup `git log` for its revision follows the same rule. A task can write `~/.gitconfig` or an executable into a `PATH` directory, so neither is read or run. The price is that a repository trusted only through a global `safe.directory` is refused. A directory above a protected tree (the `cwd`, an `allowedRoot`, a git directory) cannot be renamed or deleted either, since `rename("/tmp/review", ...)` would otherwise move the protected tree to a path no rule names. It denies reading, changing or deleting the token file, and renaming it or any directory above it, since a rename would put the file at a path the read rule does not name. It denies outbound TCP to the relay's port on any address, so the agent cannot call the API even with a token. And the relay removes `AGENT_RELAY_TOKEN` and `A2A_RELAY_TOKEN` from a sandboxed task's environment. A sandboxed agent therefore cannot delegate through the relay's MCP tools either.

The protection is deliberately narrow (not general confinement). It does not block: a hard link that already exists outside the protected directories and points into them (the sandbox matches paths, not files); writes made for the agent by another local service it can reach, such as a Docker socket, a daemon on a Unix socket or another port, or a proxy in front of the relay; reads of any other file; other network access, including the model API; writes outside the protected directories; the git metadata of a repository other than the one around the `cwd`; or reading the relay process's environment when the token came from `AGENT_RELAY_TOKEN` and not from the token file. macOS does not nest sandboxes, so an agent that applies its own (Codex) selects its own read-only mode through `args` instead. Example entries:

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

The relay limits request and output size, running task count, stored task count, and execution time. A caller-supplied `requestId` makes task submission idempotent while that task remains stored. A per-agent or per-task `cwd` selects the working directory, constrained to the agent's `allowedRoots` so a caller cannot redirect work into an arbitrary project. Result and task state live in memory for now; tasks disappear on restart and old terminal tasks can be evicted. Task snapshots leave out the prompt and carry `inputChars`, because the caller already holds it; `GET /v1/tasks/:id?includeInput=true` returns it. Clients long-poll `GET /v1/tasks/:id?waitMs=...` instead of polling in a loop. A wait that ends before the task is terminal returns the task with `waitExpired: true`, so a caller can tell an expired wait from a finished task without comparing status strings. The relay checks a `waitMs` sent with a submission before it creates the task, so a refused wait leaves no task running without an ID. The MCP `wait_task` and `delegate` clamp a wait above the relay's `maxWaitMs` to that limit instead of failing. `GET /v1/tasks?sessionId=...` recovers task IDs after a client restart. The correlation `sessionId` is deliberately separate from native harness sessions, which v1 does not expose.

A workflow is a declarative document of named steps over that same queue, not a script sandbox. `needs` forms an acyclic graph, the relay starts a step only once every step it needs has completed, and `{{stepId.output}}` threads one result into the next prompt. Because a step *is* a task, the active cap, timeouts, `allowedRoots`, options, read-only profiles, idempotency, and cancellation need no second implementation, and `DELETE /v1/workflows/:id` simply cascades to the tasks the run started. `foreach`, `retries`, and a regex-guarded `repeat` cover fan-out, transient failure, and a capped loop without handing the caller a general programming surface. Each run appends a journal to `<state dir>/workflows/<id>.jsonl`, which is what lets `POST /v1/workflows/:id/resume` re-run only the steps that never finished after a failure or a restart. That journal is deliberately narrower than durable task storage: it keeps the document and each completed step's output, not enough to trust task IDs across restarts, and a step whose task is gone fails rather than silently continuing.


Because a run can load a machine for a long time, the relay also reads host pressure: the kernel's memory pressure level, free memory, swap, and any CPU throttle macOS has recorded. The reading gates a new run (critical refuses it) and a background guard stops active runs that turn critical, which is why the journal's per-step checkpoint matters — a stopped run resumes at the step that was running, not at the beginning. Temperature in degrees is not part of the reading because macOS exposes none without root on Apple Silicon, and a fabricated number would be worse than the stated absence. The probes are macOS-only: on another host every signal is null, the verdict is `ok`, and the guard never fires, which is a stated absence rather than a healthy machine.

## Agent compatibility

- Codex: `codex exec` for new tasks. The [official OpenAI Docs](https://learn.chatgpt.com/docs/app-server) describe app-server for deeper conversation and event integration.
- Claude Code: `claude --print` for new tasks.
- OpenCode: `opencode run` for new tasks; its server API can target a specific session in a later adapter.
- Pi: `pi --print` for new tasks; its RPC mode is a later option for persistent sessions.
- DeepSeek Harness: `dsh --profile headless` for new tasks.
- Freebuff: the installed CLI exposes an interactive interface but no documented headless result mode. A reliable adapter needs a supported programmatic interface; terminal automation would be fragile.

These are adapter entry points, not a claim that each agent has been invoked end to end. The automated tests use fake commands and fake HTTP responses.

## Next increments

1. Add SQLite task storage before depending on task IDs across relay restarts.
2. Add native adapters only where needed for session continuation, event streams, and agent-specific cancellation. Keep capability flags truthful.
3. Add TLS (or document a supported HTTPS proxy setup) for non-loopback use. A bearer token now protects the API, and binding outside loopback requires `AGENT_RELAY_UNSAFE_ALLOW_NON_LOOPBACK=1`.
4. Add an A2A adapter when a concrete A2A client needs it; validate that adapter against the selected A2A protocol version.

The original project folder has no Git metadata or configured remote. The three comparison implementations were developed in isolated temporary Git worktrees.
