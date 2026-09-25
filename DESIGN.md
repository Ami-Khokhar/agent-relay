# Relay design decision

## Use case

A coding agent can delegate a **new** task to another registered agent, inspect its progress, get its result, or cancel it. The first release does not resume an existing interactive session. Every adapter declares its actual capabilities; a correlation `sessionId` is not a promise of native session continuity.

## Transport and protocol choice

| Method | Best fit | Result of the experiment |
| --- | --- | --- |
| HTTP task API | Shared relay on one host or across machines; asynchronous jobs | Chosen as the core. Submission returns a task ID; clients poll or cancel. |
| MCP over stdio | Coding agents that can discover and call tools | Chosen as a thin interface over the HTTP API. It uses `list_agents`, `delegate`, `wait_task`, `get_task`, `list_tasks`, and `cancel_task`. |
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

The relay limits request and output size, running task count, stored task count, and execution time. A caller-supplied `requestId` makes task submission idempotent while that task remains stored. A per-agent or per-task `cwd` selects the working directory, constrained to the agent's `allowedRoots` so a caller cannot redirect work into an arbitrary project. Result and task state live in memory for now; tasks disappear on restart and old terminal tasks can be evicted. Clients long-poll `GET /v1/tasks/:id?waitMs=...` instead of polling in a loop, and `GET /v1/tasks?sessionId=...` recovers task IDs after a client restart. The correlation `sessionId` is deliberately separate from native harness sessions, which v1 does not expose.

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
