# Use-case patterns

The relay is a job board, not a shared memory. Each task is a fresh invocation, and results
come back by waiting or polling. Build the user's workflow from these patterns.

Before any pattern: confirm the target CLIs exist (`which <command>`), register them, start
the relay, and verify with `GET /v1/agents`.

Throughout, "delegate" means `POST /v1/tasks` (or the MCP `delegate` tool). Prefer the
long poll — `GET /v1/tasks/:id?waitMs=<ms>` (or MCP `wait_task`) — over a `get_task` loop:
one call instead of many, and it does not stop too early. Use `GET /v1/tasks?sessionId=...`
(or `list_tasks`) to recover task IDs after an orchestrator context reset.

Sessions: a session is created by the first task that uses its `sessionId`, which records
that task's agent as metadata. By default any agent may reuse the id for cross-agent
correlation; `A2A_RELAY_STRICT_SESSION_AGENT=1` rejects reuse by a different agent with
`409 session_agent_mismatch`. Name a session with `sessionName` on the creating call, and
find earlier ones with `list_sessions`.

## 1. Single delegation

The user wants agent A to do a job on agent B.

1. Pick the agent whose `cwd` is the project B should edit, or pass a per-task `cwd` that
   is inside the agent's `allowedRoots`.
2. Submit one task with a self-contained `input` and a `requestId`.
3. `wait_task` until terminal; report `output` (or `error`).
4. If the user changes their mind, `cancel_task`.

Use a generous `timeoutMs` for real coding work. The default is 15 minutes; set a
per-agent `timeoutMs` for tasks that need longer, and `A2A_RELAY_MAX_TIMEOUT_MS` only if
you want a hard ceiling.

## 2. Fan-out and compare

Run the same prompt on several agents, then compare.

- Register each harness and submit one task per `agentId` with distinct `requestId`s.
  Give all the tasks one shared `sessionId` (a correlation tag) or omit `sessionId` —
  either works; strict session-agent mode is the only case that requires one session per
  agent.
- Keep within `A2A_RELAY_MAX_ACTIVE`; the rest queue automatically.
- `wait_task` on each task ID (or `list_sessions` afterwards to see what ran where), then
  present a table of agent → status → output.

```
sessionId = "compare-<date>"
for agent in claude codex pi:
    task = delegate(agentId=agent, input=PROMPT, sessionId=sessionId,
                    requestId=f"{agent}-compare", waitMs=600000)
```

## 3. Pipeline / chaining

A produces, B reviews, C fixes. Pass each output into the next `input`.

1. `outA = delegate(A, spec)` → `wait_task`
2. `outB = delegate(B, "Review this and list concrete problems:\n" + outA.output)` → `wait_task`
3. `outC = delegate(C, "Apply these fixes:\n" + outB.output)` → `wait_task`

Give the whole chain one `sessionId` for traceability; it is correlation data, not a
native session. If an agent is itself wired to the relay as an MCP client, it can
delegate further on its own — the relay does not need to know.

## 4. Review loop

Two harnesses checking each other:

- B reviews A's change and returns findings.
- Feed findings back to A as a new task (new `requestId`, same `sessionId`).
- Repeat until findings are empty or a max round count is hit. Cap the rounds; there is no
  built-in loop.

## 5. Wrap a harness that has no convenient CLI

If the harness exposes only an API or needs structured output:

1. Copy `examples/stdio_adapter.py`.
2. Replace `run_harness` with code that calls the harness and returns text.
3. Register it as `type: "stdio"`.
4. Test it by piping a request document (see [adapters.md](adapters.md)).

## 6. Expose delegation to an MCP client

Add the MCP server to the client's config and start the HTTP service alongside it (or let
the MCP process start it on demand). The client then sees `list_agents`, `delegate`,
`wait_task`, `get_task`, `list_tasks`, `list_sessions`, `cancel_task`. Because submission is
asynchronous, use `delegate` with `waitMs` or `wait_task` to block until the result is ready.

## Choosing limits

- `timeoutMs` per task: sized to the work. A request value wins over the agent value, which
  wins over the global default (`A2A_RELAY_TIMEOUT_MS`, 900000). A per-agent value is not
  capped by the default; `A2A_RELAY_MAX_TIMEOUT_MS` is the optional hard cap.
- `A2A_RELAY_MAX_ACTIVE`: how many run at once (default 4). Raise for fan-out, lower to
  protect a machine.
- `A2A_RELAY_MAX_OUTPUT_BYTES`: raise if agents return large reports (default 256 KiB).
- `A2A_RELAY_MAX_COMMAND_INPUT_BYTES`: raise only for `command` adapters, and stay well
  under OS argument limits; prefer `stdio` for large prompts.

## Presenting results

Report, per task: `agentId`, `status`, `output` or `error`, and `finishedAt`. Surface
`outputTruncated: true` so the user knows the result was cut off. For fan-out, a compact
table reads best. Always relay failures verbatim — the `error` field is the harness's own
message.
