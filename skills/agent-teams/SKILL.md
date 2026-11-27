---
name: agent-teams
description: Form and coordinate a mixed-harness team through discovery, prototyping, an agreed specification, bounded implementation and review, and a clear closeout.
---

# Agent teams

Use this skill when the user asks you to coordinate multiple configured Relay workers around an objective. You remain the lead in the user's existing conversation; the team board records coordination and does not replace that conversation.

## Lead workflow

1. **Understand the objective.** Ask focused questions when intent or constraints are unclear. Record the objective, constraints, known decisions, and unresolved questions. Research relevant facts and identify what evidence would resolve uncertainty.
2. **Demonstrate when evidence helps.** Build the smallest prototype that tests the risky or user-visible idea. This can be an interactive mockup, sample API, or command interaction. Say why a prototype adds little evidence when you skip it. Treat prototype code as exploratory unless it is deliberately adopted.
3. **Agree the specification.** Write testable acceptance criteria, explicit non-goals, dependencies, and unresolved questions. Get the user's agreement before starting implementation assignments. Research and prototypes can happen before agreement. Return material changes in behavior, scope, agreed intent, or success criteria to the user; do not change criteria to make current work pass.
4. **Build and review.** Decompose agreed criteria into owned assignments with dependencies and expected evidence. Dispatch only useful work. Coordinate peer discussion, integrate results, and arrange independent review where it adds value.
5. **Refine against the agreement.** Compare artifacts and evidence with the agreed criteria and user feedback. Report regressions and remaining uncertainty. Ask the user before material scope or acceptance changes.
6. **Close clearly.** Present the result, artifacts, checks, review evidence, and limitations. Close the run when work is complete and stop autonomous team activity. A new request explicitly reopens work.

The team API records these phases (`exploration`, `prototype`, `specification`, `build`, `review`, `complete`). Build requires an approved nonempty specification. Only the lead can approve it, change phase, revise assignments, or close the run.

## Build a useful team

- Inspect the configured roster, each worker's supported capabilities and options, available harnesses/models, and the run's concurrency, elapsed-time, and spending limits before assigning work. Use only workers and choices the system exposes.
- Keep **role**, **harness**, and **model** distinct. Choose a role for responsibility (for example implementation, research, or review), a harness for its interaction and editing capabilities, and a model for task fit. A model name does not imply a role or capability.
- Break the objective into bounded deliverables. Name an owner, expected artifact/evidence, and prerequisites for each assignment. Run independent research or implementation in parallel only when their dependencies and integration points are clear.
- Choose the smallest team that covers the needed work. Add a reviewer with an independent perspective for consequential or difficult changes. Do not create agents with no useful assignment, ask multiple agents to duplicate work without a reason, or make review merely ceremonial.
- Use separate Git worktrees for agents editing code. Record the assignment, worktree/branch, base revision, resulting commits, and review evidence. The lead coordinates integration and conflicts. A peer request never transfers another worker's assignment or worktree.
- Form teams autonomously from the configured roster. New harness setup, credentials, permissions, or expanded limits are not implied. Ask the user before expanding limits; otherwise fit the plan to the available budget and report constraints.
- Explain the proposed roster, roles, harnesses/models, assignment split, dependencies, and budget tradeoffs in a concise lead update. The team board enforces authority and resource limits; this skill does not grant extra access.

Use the existing roster discovery surface (`list_agents`) to inspect configured agents before calling `create_team`. Each participant has a stable team-local `id`; set `agentId` to a registered relay agent ID (it defaults to `id`). These identities may differ. Roles are any nonempty value except `lead`; use descriptive roles such as `worker`, `reviewer`, or `evaluator`. Do not invent an agent ID or imply that an unconfigured harness can be provisioned by team creation.

## Coordinate workers and discussions

- Give each worker a bounded assignment, relevant context, constraints, and a concrete definition of done. Workers own their assigned work, initiate relevant questions, respond to explicit tags, provide evidence, and request help or review.
- Route worker questions for the user through the lead. Answer questions the team can resolve; consolidate overlapping questions and ask the user only for decisions the lead cannot reasonably make. Independent work may continue while a decision is pending.
- Workers may discuss and decide implementation details within their assignments. Peer messages are proposals, evidence, or requests, not orders. Only the lead revises assignments, priorities, scope, or permissions.
- Keep discussion relevant to the assignment and thread. Use this message shape so another execution can act on it:

  ```text
  [INBOX] @recipient — assignment: <id>
  Scope: <what this message concerns>
  Request/question: <specific ask>
  Evidence/context: <facts, links, paths, or concise excerpt>
  Reply by: <bounded point, if needed>
  ```

- Reply to an addressed message with `[REPLY]`, its assignment and the relevant message/conclusion reference, then a direct answer, evidence, and any remaining uncertainty. Do not make an untagged broadcast a substitute for an addressed request.
- For substantive discussion, end with `[CONCLUSION]`: decision and rationale; evidence considered; who will act; any assignment change needed from the lead; and unresolved uncertainty. The lead records or resolves scope questions. If there is no agreement after a bounded exchange, or someone is blocked, escalate alternatives, evidence, and uncertainty to the lead instead of continuing indefinitely. The lead decides, asks for an experiment, or asks the user.
- Workers do not have a user-reply control in worker threads. The user steers the lead. Summaries help navigation but are not a complete message history or proof that a worker is currently running.

Use `post_team_message` for an addressed message, with `teamId`, `recipientId`, `body`, and when applicable `threadId`, `assignmentId`, and `kind` (`message`, `question`, `proposal`, `challenge`, `request`, or `advice`). Participants read their addressed inbox with `get_team_inbox` and acknowledge receipt with `ack_team_delivery`. These records show posting, inbox visibility, and acknowledgement; they do not guarantee the recipient acted or replied. Check the recorded thread before treating a request as answered. Use `conclude_team_discussion` with `threadId`, `summary`, `rationale`, optional `action`, and `needsLead` to record the conclusion.

## Session continuity and recovery

- Treat a participant identity as stable within a run and distinct from harness, model, role, and each execution attempt. Inspect the recorded thread for replies; a posted request is not evidence it was delivered, received, or answered.
- When a finished worker is tagged, use native session continuation only when the harness supports it and the adapter confirms it. Otherwise start a visibly marked replacement execution with the assignment and relevant recorded context. Never claim that native memory continued when a fresh execution was started.
- Relay task/workflow `sessionId` values are correlation identifiers, not native harness sessions. A task is a fresh invocation unless a supported adapter explicitly provides continuation. Do not infer continuation from matching IDs or a durable message record.
- On interruption or disconnection, check execution state and team records before retrying. Do not blindly repeat edits or other effects. Resume only work that is incomplete, and include recorded context in any replacement execution.

Execution attempts, request IDs, assignment revisions, task IDs, status, and bounded result records persist in local SQLite. Attempts are prepared atomically against the run's concurrency limit, current dependency revisions, and specification hash. Duplicate request IDs return the existing attempt and never submit a second task. Startup recovery reconciles known task IDs and marks missing or pre-submit attempts interrupted; it does not replay their effects. Large task output and execution histories are clipped with truncation or omitted-history markers so recovery records and replacement prompts remain bounded. If a replacement prompt says context was omitted or clipped, retrieve the complete specification and assignment with `get_team` before acting; do not infer missing constraints. Use full attempt/task records where available to inspect complete output. Inbox and acknowledgement records show whether an addressed event was seen, but do not prove a response or action. Native session continuation is unsupported: replacement executions include prior attempt results and relevant recorded messages, with native session resumption marked false. A Relay task `sessionId` remains a correlation value and is not native session memory.

## Budgets and stopping

- Plan within configured concurrency, elapsed-time, and spending limits. Keep peer discussions bounded by a small number of relevant exchanges and a clear question or decision point. Prefer one useful experiment or lead decision to open-ended debate.
- Do not silently raise a limit, start additional work after the run closes, or keep dispatching work after the user's objective is complete. Cancel or stop work when the user changes direction, when its result is no longer needed, or when the run closes; report any effects still in progress.
- Track actual usage where available and label unavailable values as unknown. Reserve capacity for integration and review rather than spending the entire budget on parallel implementation.

Current team limits accept `max_messages`, `max_elapsed_seconds`, and `max_concurrency`; configured values must be positive. Omitted values default to 100 discussion messages, 4 active attempts, and 3600 elapsed seconds. `max_messages` bounds discussion messages. `max_concurrency` bounds persisted active execution attempts, and `max_elapsed_seconds` is checked when assignments are written and dispatched. Spending is not tracked or enforced by these adapters; label usage and cost as unknown unless another configured source provides measurements.

## Available team operations

The MCP operations include `create_team`, `list_teams`, `get_team`, `assign_team_work`, `post_team_message`, `conclude_team_discussion`, `set_team_phase`, `close_team`, `reopen_team`, `dispatch_team_assignment`, inbox and delivery acknowledgement, attempt inspection, summary, user questions, artifacts, and Git history export. HTTP endpoints are under `/v1/teams`; inspect the MCP tool descriptions or API implementation for each operation's fields and authorization. The lead creates and manages run records. Participant credentials are provisioned out of band; they are not minted by team creation. Dispatch runs the assigned registered worker. Research and review tasks use read-only mode. Prototype and implementation tasks require an allowed source repository and base commit and run in separate worktrees. The lead explicitly dispatches initial assigned work. Later addressed discussion messages can wake an idle, previously dispatched worker with recorded context and verified worktree provenance, within the run limits. Acknowledgments never start work. Inspect attempts and inbox state rather than assuming a tag succeeded. The external lead uses get_team_inbox or bounded wait_team_messages; Relay does not wake the lead harness.

## Subjective intent and evaluation

Use the user's `intent-to-evals` skill only when the user can recognize or feel that an outcome is wrong but cannot explain what makes it wrong. Do not invoke this process for requirements with direct, reliable checks.

Offer small contrasting examples, ask which feels closer and why, state a hypothesis about the difference, then test it on fresh examples. The user supplies taste; you discover and validate checks. The user can reject both examples or combine them.

For each useful check, record inputs, procedure, grading rule, failure evidence, and failures that outweigh a better score elsewhere. Confirm it distinguishes an accepted example from a plausible near miss, and look for an output it might reward that the user would reject. Keep preferences separate while their tradeoff is unresolved. Refine against the same baseline under the same conditions, report regressions, account for scoring variation, and confirm with fresh cases. If evaluation remains unreliable, keep human judgment as the acceptance check. Intent or success-criterion changes require user agreement and re-evaluation of the baseline; workers cannot redefine success to pass.
