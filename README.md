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

Configure that command as a local stdio MCP server in the client. It provides `check_updates`, `prepare_feedback`, `list_agents`, `delegate`, `wait_task`, `get_task`, `list_tasks`, `cancel_task`, `run_workflow`, `get_workflow`, `list_workflows`, `cancel_workflow`, `resume_workflow`, and `get_pressure`. Initialization may show one cached release version and canonical link; it reads only local metadata. `check_updates` fetches bounded release metadata only when explicitly called and returns notes as data. Neither path installs or restarts anything. `A2A_RELAY_URL` overrides the HTTP address it calls. When the relay is unreachable and the URL is loopback, the MCP process starts the HTTP service detached on first use and logs it to `~/.local/state/agent-relay/relay.log`; disable that with `A2A_RELAY_AUTOSTART=0`.

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
bash scripts/install-service.sh          # launchd on macOS, systemd --user on Linux; also installs the metadata-only release-check timer
bash scripts/install-service.sh --no-update-timer   # the service only, no scheduled release check
bash scripts/install-service.sh --uninstall
```

The default install schedules a release check that runs the checkout's
`src/release_discovery.py`, so a checkout missing that file is rejected before
anything is created or scheduled; rerun `bash
skills/agent-relay/scripts/setup.sh` to refresh the checkout, or pass
`--no-update-timer` to install the service only.

A service does not inherit your login shell `PATH`. The installer writes a `PATH` that includes `~/.local/bin`, `~/bin`, `/opt/homebrew/bin`, and `/usr/local/bin` so agent CLIs resolve; override with `AGENT_RELAY_PATH`.

## Check for a release

Run `python3 src/release_discovery.py` to compare the installed version with published stable GitHub releases. Add `--include-prereleases` to include prereleases, or `--notify` to print each offered version once using a private device-local cache. On POSIX, the cache uses restrictive directory and file permissions; elsewhere it relies on operating-system user-cache defaults. The check reads only canonical GitHub release metadata over HTTPS; it does not download release content or restart the relay. With `--notify`, it writes the private notice cache under `~/.cache/agent-relay` (or the absolute `XDG_CACHE_HOME` directory). It prints bounded release notes and the canonical release page. An empty release list is a normal no-update result. The project currently publishes no releases or tags. Package-managed installs should use their package manager's documented commands; this release check does not claim package-manager update support.

## Inspect a release and approve it on this device

`python3 src/safe_update.py inspect 1.2.3` resolves one exact stable release identity from the canonical GitHub releases API: the tagged release, the immutable 40-hex commit peeled from `refs/tags/` (lightweight or annotated; `target_commitish` is never proof), the `agent-relay-manifest.json` asset and its sha256 digest, and the release/asset timestamps. It then validates the manifest (`agent-relay.update/v1`), which binds the repository, version/tag and commit plus a path/mode/SHA256 inventory of every tracked file. Anything noncanonical — a different repository, a draft or prerelease, a redirect to another host, a missing or mutated manifest — is refused; no release matches is a clear refusal. The manifest asset is requested with `Accept: application/octet-stream`; GitHub answers that with one redirect to a signed canonical download host, which alone may be followed — metadata endpoints follow no redirect at all. `inspect` is metadata only: it writes nothing and runs nothing.

`python3 src/safe_update.py approve 1.2.3` requires a real interactive terminal and asks you to type the exact version and the full 40-character commit. On a match it records a private device-local consent file (mode 0600, owned by you, under a lock file, bound to the canonical installation realpath, a locally generated device id, the full identity digest, a fresh nonce, and a 15-minute expiry). `python3 src/safe_update.py consume 1.2.3` validates that record — rejecting expired, future, replayed, mutated, wrong-device or changed-path consent — and burns it. Nothing is installed, activated or restarted by any safe-update command; the manifest digest authenticates content only because canonical GitHub maintainers publish it over HTTPS, not through a separate signature. Later stages of the update work will consume the consent through the same CLI.

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

### The update drain (rehearsal)

`/v1/admin/update/drain` is the shared lock a safe update holds across an activation: while it is held, the relay refuses every new external admission — task submissions, workflow submissions and resumes — but already accepted work finishes normally: running tasks, queued tasks, and the child tasks of live workflows (whose engine uses an internal admission path no request field can select). It needs the token and is accepted only from loopback.

- `POST` acquires it: body `{"transactionId": "<name>", "revision": "<40-hex commit of this running relay>"}`. It answers `200` with an opaque `capability` (a token only the relay mints), `held: true`, and the counts that are still running; `409 drain_held` when another transaction already holds it, `409 revision_mismatch` when the body does not name this relay's exact commit, `400` for malformed fields. The acquire and the admission gate are decided under the same server lock, so a racing submission either lands fully or refuses with `503 drain_active`; an idempotent replay of a known `requestId` still returns its prior answer.
- `GET` asks the holder's status: query `transactionId`, `revision` and `capability`. Only the relay's own answer says whether the held drain is `quiesced` (`active` adapter slots, including cancelled adapters still running, `queued` accepted tasks, and each nonterminal workflow once — a run in a step gap, a retry delay or awaiting fanout still counts). A wrong or replayed capability answers `403 capability_mismatch`, a wrong transaction or revision `409`, a free drain `409 drain_not_held`, and malformed input `400`; no error ever reads as idle, and a status answer never carries a new token.
- `DELETE` releases it: body `{"transactionId": "...", "revision": "...", "capability": "..."}` with the exact held pair. Admission reopens at once; there is no expiry that reopens it meanwhile, so a lost capability keeps the hold until its explicit release.

`python3 src/safe_update.py drain 1.2.3 --install-dir PATH` is the rehearsal consumer: it verifies the recorded stage receipt and the consumed approval consent for the exact release, acquires the drain, waits a bounded time (`--base-url` or `AGENT_RELAY_URL`/`A2A_RELAY_URL`; token from `AGENT_RELAY_TOKEN`) until the server acknowledges quiescence, prints the result and releases. It installs nothing and calls no service control.

### Prepare an activation transaction without touching the running relay

`python3 src/safe_update.py prepare-activation 1.2.3 --install-dir PATH` is the first durable stage of activation, and it still installs nothing, activates nothing and calls no service control. It revalidates everything again at the effect boundary — the currently published release identity and manifest bytes must still equal what was approved and staged, the stage must still verify against its own bound manifest, the original checkout must still be clean and canonical at the approved commit, and the durable single-use consent record (burned exactly once by `stage`) must still be intact — and only then acquires the exact-revision drain and writes a private, atomic, fsynced activation transaction journal (`<state dir>/update/transaction.json`) plus an admission barrier (`<state dir>/update/admission-barrier.json`) that binds the transaction, the old exact commit, the target identity, the stage receipt and inventory, hashed references to the external state it relies on, and the drain ownership (including the opaque capability, never the relay token). The drain is held after the command returns.

At startup the relay reads this private update state before serving any work: while a transaction is live — or the state is unreadable, malformed, torn, stale or bound to another device/install — every new external admission (tasks, workflow submissions, resumes, including from an autostarted relay or MCP client) refuses with `503 update_barrier_active`, so a crash can never silently reopen admission inside an activation gap. A relay restart no longer reopens admission once a transaction is prepared.

### Activate a prepared transaction through the restricted service boundary

`python3 src/safe_update.py activate <transaction-id> --install-dir PATH` is the first activation consumer. Before any effect it re-proves the hashed external state references, the relay's exact starting revision, the held, quiesced drain of this transaction, the verifying stage, the clean canonical checkout at the prepared revision, and the per-user service boundary. That boundary accepts only positively validated definitions — a macOS launchd `com.agent-relay.plist` or a systemd `--user` `agent-relay.service` — whose plain, non-symlinked, user-owned definition names this install's own `src/server.py` under the interpreter and working directory, with external configuration (configuration inside the checkout, shell wrappers, sudo, or systemd `ExecStartPre`-style directives are refused). Original definitions, PATH, port and configuration are preserved; the boundary runs only the plain per-user manager verbs (`launchctl bootout`/`bootstrap` on the login user's GUI domain, or `systemctl --user restart`), never as root, never an installer, a pull, a force mode, and never a kill whose quiescence is not already held.

Then, inside the transaction/install lock, the flow walks the durable journal through `draining` → `quiescent` → `activating` → `restarted`: it records the rollback metadata (the old commit and the exact service definition it will restart) before the single source effect, keeps the previous commit object available (also as `refs/agent-relay/update-rollback/<transaction>`), and swaps the checkout with trusted, hook- and filter-disabled git by fetching the staged object locally and checking out the exact staged commit detached. The restarted relay must then report the exact full target commit through an authenticated endpoint, and the new relay must still name this transaction's admission barrier — a mere `ok:true` or an open port is never a verdict. Only then does the journal record the terminal `activated` phase, the barrier and the record are expunged and admission reopens.

Any failed or uncertain step writes a durable `recovery_required` end and keeps the barrier closed (failures before the first effect leave the prepared transaction recoverable with `recover-abort`), and is then recovered automatically to the exact prior revision the journal names: the rollback re-proves the hashed external references and the held drain, restores the journaled prior commit only when the checkout still inspects clean (never overwriting an intervening user edit), restarts the same inspected service boundary only when the running relay does not already report the restored revision, and holds the barrier until the authenticated endpoint again reports that exact prior revision. A rollback step that itself fails keeps the `recovery_required` record, its barrier and an actionable remedy; the operator finishes it with `recover`.

`/v1/admin/update/state` (authenticated, loopback) reports whether the running relay holds admission closed through the persisted activation barrier, and re-reads the private barrier state from disk on an authenticated POST; the activation flow uses both to verify the restarted relay.

`python3 src/safe_update.py status` prints the recorded transaction and its evidence; nothing else is implied. `python3 src/safe_update.py recover <transaction-id>` rolls a failed (`recovery_required`) activation back through the same restricted boundaries `activate` used — the journal is the only source of the exact prior revision, the record is finalized durable and read-only once the restored revision is verified, and repeated recovery never repeats a service action or reuses a consent; it is idempotent on an already rolled-back record. `python3 src/safe_update.py recover-abort <transaction-id>` is the only operator path back to open admission: it proves, in order, that the recorded external state is byte-unchanged, that the relay still runs the transaction's exact starting revision, that this transaction still owns the held drain (by its stored revision and capability), and that the relay has quiesced — then it records the `rolled_back` phase, releases the drain with its own capability and expunges the record. Because a held drain lives only in server memory, a relay restarted with the barrier closed answers `drain_not_held` instead: after the same revision and unchanged-state proofs, that answer is accepted as proof that the hold no longer exists, so admission reopens without a release. Any failed proof is a refusal for manual recovery that leaves the barrier closed; nothing is ever reopened on a guess.


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

Add `"options": {"<name>": "<value>"}` to choose among the options the agent declares (see [Per-task options](#per-task-options)), and `"readOnly": true` to require a run that cannot change files (see [Read-only tasks](#read-only-tasks)). Submission returns `202` with a task ID and session ID. Poll the task URL for `queued`, `running`, `completed`, `failed`, `timed_out`, or `cancelled`. `GET /v1/tasks/TASK_ID?waitMs=<ms>` long-polls until the task is terminal or the wait elapses. A `waitMs` that is not an integer from 1 to `A2A_RELAY_MAX_WAIT_MS` (default 600000) returns `400 invalid_wait`, and its message names that range. If the wait elapses while the task is still `queued` or `running`, the `200` response carries `waitExpired: true`, `waitedMs` (the wait that elapsed), and a `hint` to wait again; a terminal task carries none of these. `POST /v1/tasks` also accepts `waitMs` and checks it the same way before it creates the task, so a refused wait on a new submission leaves no task behind. The check sits after the idempotent-replay decision: a `requestId` that matches a stored task with identical fields replays it (and never duplicates the work), so the replay ignores an invalid `waitMs` — the wait is advisory and never stored — while a replay with changed fields still returns `409`. The MCP `wait_task` tool clamps a `maxWaitMs` above the relay's limit to that limit (read from `/healthz` `limits.maxWaitMs`) and adds `waitMs` and `waitClamped: true` to the result; the MCP `delegate` tool does the same for `waitMs`, retrying once with the limit, and clamps the follow-up long poll the same way when that poll's wait is refused and `/healthz` reports a usable limit — including after a `requestId` replay returns `200` without re-checking the wait. If the submission created the task but `delegate`'s follow-up long poll then fails with no clamp available — a relay that refuses that wait while its `/healthz` reports no usable limit, an unreachable `/healthz`, or a transport error — `delegate` returns the created task with `waitFailed: true` and a `waitError` naming the failure and pointing at `wait_task` with the same `taskId`; it never resubmits after a successful submission. Claude Code moves an MCP call that runs longer than 120 s to the background, so with it use a `maxWaitMs` of about 110000 or less and call `wait_task` again while `waitExpired` is true. `GET /v1/tasks?sessionId=...&status=...&limit=...` lists stored tasks (most recent first). Supply a unique `requestId` and reuse it when retrying a submission; the relay returns the original task instead of starting duplicate work. Reusing it with different task fields returns `409`. Without a request ID, callers must not automatically retry a submission whose response was lost. The session ID currently groups work for the caller; generic command adapters do not continue native agent conversations.

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
| `failIfOutputMatches` | A Python regular expression. When a completed task's output matches it, the step fails with `output_matched_fail_pattern` and its dependents are skipped — an agent that reports `BLOCKED: ...` while exiting 0 cannot feed a later step. Each task's own output is tested: on a `foreach` step every item is matched separately, never the JSON array the step aggregates, so `^BLOCKED` fails the step for one blocked item and `^\[` cannot fail a fan-out on the serialization alone. A malformed pattern is refused at submit time. |
| `timeoutMs` / `cwd` | Per step, checked exactly like a task submission, including `allowedRoots`. |
| `options` | `{"<name>": "<value>"}` for that agent, exactly as for a task submission. An option the agent does not declare fails the step with `unknown_option` or `invalid_option_value`. |
| `readOnly` | `true` runs that step read-only, exactly as for a task submission. An agent with no read-only profile fails the step with `read_only_unsupported`. |

Submission returns `202` with a run ID and a step list; `GET /v1/workflows/:id?waitMs=<ms>` long-polls until the run is terminal or the wait elapses, and a wait that expires reports `waitExpired: true` exactly as a task wait does. `GET /v1/workflows` lists runs. Every step carries `startedAt` and `finishedAt`, both `null` until the step starts and finishes — a step that never ran keeps them `null`. A full run view also carries `attemptTimes`, one `{startedAt, finishedAt}` window per attempt, so a `retries` or `repeat` step reports each try separately instead of only the run's own window. A step whose dependency did not succeed is `skipped`, and the run finishes as `failed` with `error` naming the first bad step and, when that step failed, the short cause the agent printed last (the last non-empty lines of its stderr and output, with `agent_quota: ` in front when they name a usage limit or rate limit); the failing task's full stderr stays on the step as `errorDetail`, in a single-run view only. A step may set `needsPolicy: "any"` instead of the default `"all"`: it starts once every need is terminal and at least one completed — the common case for a review that should survive one timed-out branch of a parallel fan-out — and in its rendered input `{{id.output}}` of a failed or timed-out need becomes a marker naming that need's status and error; with no completed need the step is skipped with `dependency_not_completed` as today. When an orchestrator only needs to know which step finished, `?view=summary` leaves out the `spec` and every full output: each step then carries `outputChars` and `firstLine` (with its identity, status, attempts and time windows) instead, so a long-poll that ends does not re-deliver every transcript. `?step=<stepId>` fetches that one step alone with its full output and `errorDetail`, and no `spec`; both selectors combine with `?waitMs=`. An unknown `view` is `400 invalid_view`, a malformed `step` id is `400 invalid_step`, and a step the run does not have is `404 unknown_step`. `DELETE` cancels the run and every task it has started. `requestId` gives a submission the same idempotency it has for tasks. Every child task shares the run's `sessionId` unless the document sets one.

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

Manual updating with `bash scripts/update.sh` fast-forwards the source checkout at `~/.local/share/agent-relay` to `main` and restarts the service only when code changed, then verifies `/healthz`. Tasks and sessions live in memory, so the first restart is skipped while tasks are in flight; the deferral is recorded and the next run escalates unless tasks are visibly busy. `--force` restarts immediately and `--no-restart` records a deferral without restarting. The installer now schedules a metadata-only release check every 6 hours (`AGENT_RELAY_UPDATE_INTERVAL_SECS` overrides the period); it refuses to install anything when the checkout lacks `src/release_discovery.py`, so refresh with `bash skills/agent-relay/scripts/setup.sh` or pass `--no-update-timer`. Older installed timers still run `scripts/update.sh` until you rerun `bash scripts/install-service.sh` to replace their definitions. The timer's unit sets only `PATH`: the checker never gets the relay port or the checkout path, and it never calls `scripts/update.sh`; it stores a bounded offer in the local cache for MCP initialization and never pulls source, installs a release, or restarts the relay. Pass `--no-update-timer` to remove the release-check timer and keep manual updates; it also removes a timer left by an earlier install and leaves the relay service alone. `--with-update-timer` is still accepted because the timer is the default. `install-service.sh --uninstall` removes the service and the release-check timer. Automatic source installation and owner consent are separate work and are not provided here.

`GET /healthz` reports the effective limits, and `GET /v1/agents` reports each agent's effective `timeoutMs`. Both also report the deployed `revision`, and so does the MCP `list_agents` result (it returns the `GET /v1/agents` payload):

```json
{"revision": {"commit": "131c0c0a3be70175e4bedaf4ef6074d0527f9cb9", "short": "131c0c0", "date": "2026-10-02T19:10:52Z"}}
```

It names the git commit that the relay process started from and that commit's date (ISO 8601, in the committer's time zone). The relay reads it once at startup, with its trusted git and minimal environment (see "The git the relay runs"), so a pulled update shows only after the restart: a `date` that is weeks old means the running relay is stale. Each field is `"unknown"` when the relay does not run from a git checkout or git cannot answer; that never stops the relay from starting. A relay older than this field omits `revision`.

Run `python3 -m unittest discover -s test -p 'test_*.py'` to exercise the relay and MCP interface. The tests use fake `command`, `stdio`, and `http` adapters and never call a real agent. The CLI entry points in the registry example (`claude --print`, `codex exec`, `pi --print`, `opencode run`) are starting points, not tested integrations: verify each against your installed version with `bash scripts/smoke.sh <agentId>`.
