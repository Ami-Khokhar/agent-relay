# Use-case patterns

The relay is a job board, not a shared memory. Each task is a fresh invocation, and results
come back by polling. Build the user's workflow from these patterns.

Before any pattern: confirm the target CLIs exist (`which <command>`), register them, start
the relay, and verify with `GET /v1/agents`.

Throughout, "delegate" means `POST /v1/tasks` (or the MCP `delegate` tool) followed by
polling `GET /v1/tasks/:id` (or `get_task`) until the status is terminal.

## 1. Single delegation

The user wants agent A to do a job on agent B.

1. Pick the agent whose `cwd` is the project B should edit.
2. Submit one task with a self-contained `input` and a `requestId`.
3. Poll until terminal; report `output` (or `error`).
4. If the user changes their mind, `cancel_task`.

Use a generous `timeoutMs` for real coding work (the default is 120s, often too short).

## 2. Fan-out and compare

Run the same prompt on several agents, then compare.

- Register each harness, submit one task per `agentId` with the **same** `sessionId` value
  (a correlation tag) and distinct `requestId`s.
- Keep within `A2A_RELAY_MAX_ACTIVE`; the rest queue automatically.
- Poll all task IDs, then present a table of agent → status → output.

```
sessionId = "compare-<date>"
for agent in claude codex pi:
    delegate(agentId=agent, input=PROMPT, sessionId=sessionId, requestId=f"{agent}-compare")
```

## 3. Pipeline / chaining

A produces, B reviews, C fixes. Pass each output into the next `input`.

1. `outA = delegate(A, spec)`
2. `outB = delegate(B, "Review this and list concrete problems:\n" + outA.output)`
3. `outC = delegate(C, "Apply these fixes:\n" + outB.output)`

Give the whole chain one `sessionId` for traceability. If an agent is itself wired to the
relay as an MCP client, it can delegate further on its own — the relay does not need to know.

## 4. Review loop

Two harnesses checking each other:

- B reviews A's change and returns findings.
- Feed findings back to A as a new task (new `requestId`, same `sessionId`).
- Repeat until findings are empty or a max round count is hit. Cap the rounds; there is no
  built-in loop.

## 5. Wrap a harness that has no convenient CLI

If the harness exposes only an API or needs structured output:

1. Copy `examples/stdio-adapter.mjs`.
2. Replace `runHarness` with code that calls the harness and returns text.
3. Register it as `type: "stdio"`.
4. Test it by piping a request document (see [adapters.md](adapters.md)).

## 6. Expose delegation to an MCP client

Add the MCP server to the client's config and start the HTTP service alongside it. The
client then sees `list_agents`, `delegate`, `get_task`, `cancel_task`. Because submission
is asynchronous, instruct the client to poll `get_task` until terminal.

## Choosing limits

- `timeoutMs` per task: sized to the work; the effective value is
  `min(request.timeoutMs ?? agent.timeoutMs ?? global, global)`.
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
