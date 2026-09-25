# agent-relay: setup feedback and improvement checklist

Source: a first-time setup by Claude Code on 2026-09-23, against `main` at `97271b4` (Node.js
version). The `migrate-to-python` branch is not merged. Apply each item to the branch that
becomes canonical.

Setup rating: 7/10. The core worked on the first try: zero dependencies, 24/24 tests pass,
and a real task through Codex completed in 11 s. Most of the difficulty is after the code
runs: keeping the service alive, pointing agents at the right project, and long tasks.

## P0: blocks real use

- [ ] **Per-agent `timeoutMs` is silently capped by the global timeout.** In
  `src/server.mjs:221`, `Math.min(request.timeoutMs ?? agent.timeoutMs ?? timeout, timeout)`
  caps every task at `A2A_RELAY_TIMEOUT_MS` (default 120000). An agent registered with
  `"timeoutMs": 900000` gets 120000, with no warning. Fix: let a per-agent value raise the
  limit (or reject at load time a per-agent value above the global cap, with a clear error).
  Add a test: agent `timeoutMs` above the global default is applied to the task.
- [ ] **The default timeout of 2 minutes is too short for coding agents.** Real
  review/fix tasks take 5–20 minutes. Raise the default (for example 15 minutes) or document
  the override in the "Run" section, not only in the limits paragraph.
- [ ] **No way to keep the relay running.** The MCP server is useless unless the HTTP
  server is already up, and nothing starts it. Pick one:
  - have `mcp-server.mjs` start the HTTP server on demand when `/healthz` fails (simplest
    for the user: register one MCP command and it works), or
  - ship `scripts/install-service.sh` that writes a launchd plist (macOS) / systemd user
    unit (Linux), with an absolute `node` path and a `PATH` that includes the agent CLIs.
  Document the `PATH` issue: services do not inherit the shell `PATH`, so `codex`, `pi`,
  `dsh` (in `~/.local/bin` or `/opt/homebrew/bin`) are not found.
- [ ] **`cwd` is fixed per agent, not per task.** To send work to Codex in project A and
  then project B, the user must register two agents and restart the relay. Add an optional
  `cwd` to `POST /v1/tasks` and to the MCP `delegate` tool, constrained to an allowlist of
  roots in the agent entry (for example `"allowedRoots": [...]`). Reject paths outside it.

## P1: friction during setup

- [ ] **The MCP `delegate` tool has no wait option.** The orchestrator must loop on
  `get_task`, which costs one tool call per poll. Add `wait_task { taskId, maxWaitMs }`
  (long-poll on the server, return when terminal or at `maxWaitMs`), or a `wait` flag on
  `delegate`.
- [ ] **Give a one-line MCP registration for each client.** For Claude Code:
  `claude mcp add --scope user agent-relay -e A2A_RELAY_URL=http://127.0.0.1:43124 -- node /abs/path/src/mcp-server.mjs`.
  Add the Codex (`~/.codex/config.toml`) and OpenCode equivalents too.
- [ ] **`scripts/setup.sh` clones into the current directory by default.** From a skill,
  the current directory is often an unrelated project. Default `AGENT_RELAY_DIR` to a stable
  path (for example `~/.local/share/agent-relay`), and print the MCP registration command
  with the resolved absolute path.
- [ ] **The skill install is not automated.** Add a step (or a flag in `setup.sh`) that
  links `skills/agent-relay` into `~/.claude/skills/` (and the Codex skills directory).
- [ ] **Make the example registry correct for current CLIs.** `codex exec` fails outside a
  git repo unless `--skip-git-repo-check` is given. State that `codex exec` runs with a
  read-only sandbox by default, and show the flag to allow writes (`-s workspace-write`).
  Mark which example flags were verified, and with which CLI versions.
- [ ] **Registry location.** `config/agents.json` lives inside the source checkout. Also
  read `~/.config/agent-relay/agents.json` when present, so a reinstall or second clone
  does not lose the registry.
- [ ] **Reload the registry without a restart** (`SIGHUP` or `POST /v1/admin/reload`,
  loopback only). Today every registry edit needs a restart, which drops in-memory tasks.

## P2: robustness and polish

- [ ] **Add `GET /v1/tasks?sessionId=...`** (and an MCP `list_tasks`). After an
  orchestrator context reset, task IDs are lost and results cannot be recovered.
- [ ] **Show the effective limits** in `/healthz` or `/v1/agents` (timeout per agent, max
  active, max input size), so a caller can see the 120 s cap before it submits.
- [ ] **Add a `scripts/smoke.sh <agentId>`** that submits "Reply with exactly PONG", polls,
  and prints the result. This is the check that proved the setup; ship it.
- [ ] **Name consistency.** The repo is `agent-relay`, the local folder is `a2a-relay`, env
  vars are `A2A_RELAY_*`, and the MCP `serverInfo.name` is `a2a-relay-http-mcp`. Pick one
  name; keep the old env vars as aliases.
- [ ] **Resolve Node vs Python.** `main` is Node; `migrate-to-python` rewrites it. The
  README, SKILL.md, and `setup.sh` all assume Node. Merge or close the branch, then update
  the docs in the same change.
