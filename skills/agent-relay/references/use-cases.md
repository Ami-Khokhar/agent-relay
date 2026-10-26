# Use-case patterns

The relay is a job board, not a shared memory. Each task is a fresh invocation, and results
come back by waiting or polling. Build the user's workflow from these patterns.

Before any pattern: confirm the target CLIs exist (`which <command>`), register them, start
the relay, and verify with `GET /v1/agents`.

Throughout, "delegate" means `POST /v1/tasks` (or the MCP `delegate` tool). Prefer the
long poll — `GET /v1/tasks/:id?waitMs=<ms>` (or MCP `wait_task`) — over a `get_task` loop:
one call instead of many, and it does not stop too early. Use `GET /v1/tasks?sessionId=...`
(or `list_tasks`) to recover task IDs after an orchestrator context reset.

Keep each wait short, and loop. A wait that ends before the task is terminal returns the
task with `waitExpired: true` and a still `queued` or `running` status. That is not a
result: wait again with the same task ID until a result has no `waitExpired`. A client with
a short tool-call budget (Claude Code moves an MCP call that runs longer than 120 s to the
background) uses a `maxWaitMs` of about 110000 or less. The relay refuses a wait above
`A2A_RELAY_MAX_WAIT_MS` (default 600000); the MCP `wait_task` and `delegate` cut it to that
limit.

## 1. Single delegation

The user wants agent A to do a job on agent B.

1. Pick the agent whose `cwd` is the project B should edit, or pass a per-task `cwd` that
   is inside the agent's `allowedRoots`.
2. Submit one task with a self-contained `input` and a `requestId`.
3. `wait_task` with `maxWaitMs` of about 110000 or less. Call it again with the same
   task ID while the result has `waitExpired: true`. Report `output` (or `error`) from the
   terminal result.
4. If the user changes their mind, `cancel_task`.

Use a generous `timeoutMs` for real coding work. The default is 15 minutes; set a
per-agent `timeoutMs` for tasks that need longer, and `A2A_RELAY_MAX_TIMEOUT_MS` only if
you want a hard ceiling.

## 2. Fan-out and compare

Run the same prompt on several agents, then compare.

- Register each harness, submit one task per `agentId` with the **same** `sessionId` value
  (a correlation tag) and distinct `requestId`s.
- Keep within `A2A_RELAY_MAX_ACTIVE`; the rest queue automatically.
- `wait_task` on each task ID (or `list_tasks` for the session), then present a table of
  agent → status → output. Submit every task first and wait after: a `waitMs` on each
  `delegate` would make the agents run one after another.

```
sessionId = "compare-<date>"
tasks = []
for agent in claude codex pi:
    tasks.append(delegate(agentId=agent, input=PROMPT, sessionId=sessionId,
                          requestId=f"{agent}-compare"))     # returns at once
for task in tasks:
    while True:
        task = wait_task(taskId=task.id, maxWaitMs=110000)
        if not task.waitExpired:
            break                                            # terminal: read output or error
```

## 3. Pipeline / chaining

Prefer `run_workflow` (or `POST /v1/workflows`) for a declared chain. The relay
waits for dependencies and passes their output into each prompt:

```json
{
  "name": "write-review-fix",
  "steps": [
    {"id": "write", "agentId": "claude", "input": "Fix the failing test"},
    {"id": "review", "agentId": "codex", "needs": ["write"],
     "input": "Review the change. Implementation summary:\n{{write.output}}"},
    {"id": "fix", "agentId": "claude", "needs": ["review"],
     "input": "Apply the review findings.\nImplementation:\n{{write.output}}\nReview findings:\n{{review.output}}"}
  ]
}
```

Configure each agent to use the intended checkout, or set `cwd` on each step inside
that agent's `allowedRoots`. Passing output does not copy changed files between
checkouts. Wait with `get_workflow`, and wait again while `waitExpired` is true.
A failed dependency skips downstream steps. A journaled run can be resumed without
repeating completed steps. See [workflow API](api.md) for fields and statuses.

Use separate task submissions when the caller must inspect a result and decide
which agent or prompt comes next. Give those tasks the same `sessionId` and distinct
`requestId` values. An agent the relay started can also delegate through the lineage
policy described in the README.

## 4. Review loop

The chain above performs one review and one correction. For a fixed number of
alternating reviews and corrections, declare more steps with `needs`. For a loop
whose next action depends on interpreting findings, keep that decision in the caller
and cap the rounds.

A workflow step also supports `repeat: {"max": 3, "untilOutputMatches": "^PASS$"}`.
This repeats that step until its output matches the regex or the attempt limit is
spent. It does not repeat the whole write-review-fix chain, and a matching string is
not independent verification of the change. Use `retries` for failed or timed-out
tasks instead. Repeated or resumed work may repeat partial file changes made by an
unfinished attempt, so make those operations safe to run again.

## 5. Wrap a harness that has no convenient CLI

If the harness exposes only an API or needs structured output:

1. Copy `examples/stdio_adapter.py`.
2. Replace `run_harness` with code that calls the harness and returns text.
3. Register it as `type: "stdio"`.
4. Test it by piping a request document (see [adapters.md](adapters.md)).

## 6. Expose delegation to an MCP client

Add the MCP server to the client's config and start the HTTP service alongside it (or let
the MCP process start it on demand). The client then sees the task tools (`list_agents`,
`delegate`, `wait_task`, `get_task`, `list_tasks`, `cancel_task`) and the workflow tools
(`run_workflow`, `get_workflow`, `list_workflows`, `cancel_workflow`, `resume_workflow`) and
`get_pressure`. Because submission is asynchronous,
use `wait_task` (or `delegate` with `waitMs`) to block until the result is ready. Use a wait
of about 110000 ms or less, and wait again while the result has `waitExpired: true`.

## Choosing limits

- `timeoutMs` per task: sized to the work. A request value wins over the agent value, which
  wins over the global default (`A2A_RELAY_TIMEOUT_MS`, 900000). A per-agent value is not
  capped by the default; `A2A_RELAY_MAX_TIMEOUT_MS` is the optional hard cap.
- `A2A_RELAY_MAX_ACTIVE`: how many run at once (default 4). Raise for fan-out, lower to
  protect a machine.
- `A2A_RELAY_MAX_OUTPUT_BYTES`: raise if agents return large reports (default 256 KiB).
- `A2A_RELAY_MAX_COMMAND_INPUT_BYTES`: raise only for `command` adapters, and stay well
  under OS argument limits; prefer `stdio` for large prompts.
- `AGENT_RELAY_MAX_DELEGATION_DEPTH` (default 2) and `AGENT_RELAY_MAX_DELEGATED_TASKS`
  (default 20): cap delegation by agents the relay started. Raise the task budget when a
  spawned orchestrator fans out to more than 20 tasks. A task whose agent is waiting on a
  delegated child still holds an `A2A_RELAY_MAX_ACTIVE` slot, so nested or fan-out
  orchestration from a relay task needs a higher `A2A_RELAY_MAX_ACTIVE` too.

## Presenting results

Report, per task: `agentId`, `status`, `output` or `error`, and `finishedAt`. Surface
`outputTruncated: true` so the user knows the result was cut off. For fan-out, a compact
table reads best. Always relay failures verbatim — the `error` field is the harness's own
message.
