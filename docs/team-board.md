# Agent Relay team board

The team board is a local coordination surface for a lead and configured Relay workers. The lead stays in the existing Claude Code, Codex, or other harness conversation. The board records the run, assignments, discussions, execution attempts, questions, and artifacts; it does not replace that conversation or provide a live agent transcript.

Open `http://<relay-host>/board/` in a browser. The page is served by the Relay process. Enter the Relay owner bearer token to list and inspect runs. The page keeps the token in memory for that tab, sends it only to the same origin, and does not put it in a URL or save it. The board is a read-only view; use MCP or the HTTP API to change run records and dispatch work.

## Form a team

Use the `agent-teams` skill in the lead's existing harness. It guides the lead through understanding the objective, deciding whether a prototype would reduce uncertainty, agreeing testable acceptance criteria with the user, choosing configured workers, splitting work into bounded assignments, and closing with evidence and limitations. The skill does not provision a new agent, credential, harness, or permission. Inspect configured agents with the existing `list_agents` operation before creating a team.

The owner creates a run with `create_team` over MCP or `POST /v1/teams` over HTTP. Supply an objective, configured participants, and optional limits. A participant has a run-local `id`, a configured Relay `agentId`, and optional role, harness, and model labels. Keep identity, responsibility, harness, and model distinct. Available limits are `max_concurrency`, `max_elapsed_seconds`, and `max_messages`; values must be positive. Omitted values default to 4 active attempts, 100 discussion messages, and 3600 elapsed seconds. Existing runs acquire missing defaults on migration. Cost and spending are not measured or enforced by these adapters and should be reported as unknown.

Creating a team only creates coordination records. It does not start workers. MCP provides `create_team`, `list_teams`, `get_team`, `assign_team_work`, `post_team_message`, `conclude_team_discussion`, `set_team_phase`, and `close_team`. Additional MCP tools provide dispatch, bounded inbox waiting, acknowledgments, questions, summaries, artifacts, attempt inspection, and history export. The HTTP endpoints are described below.

## Phases and approval

Runs move through `exploration`, `prototype`, `specification`, `build`, and `review`; closing a run records `complete`. The lead records phase and specification through `set_team_phase` or `POST /v1/teams/{teamId}/phase`. Only the lead can change phase or approve a specification. Build assignments require a nonempty approved specification. Revising the specification clears approval; obtain the user's agreement again before dependent implementation proceeds.

Assignments support purpose values `research`, `prototype`, `implementation`, and `review` in the execution store. Their phase gates are respectively exploration/specification/review, prototype, build, and build/review. HTTP `POST /v1/teams/{teamId}/assignments` accepts a `purpose` field and defaults it to `implementation`. MCP `assign_team_work` accepts the same `purpose` values and default. Research and prototype work are exploratory unless deliberately adopted. Do not treat a peer discussion as approval to change agreed scope or criteria.

## Assign, dispatch, and edit safely

`assign_team_work` / `POST /v1/teams/{teamId}/assignments` creates or revises a participant's assignment and optional dependencies. Only the lead may do this. A dependency must complete at the revision recorded by the dependent assignment; revising the prerequisite makes that dependency stale until the dependent assignment is revised.

The owner dispatches with `POST /v1/teams/{teamId}/dispatch`, including `assignmentId` and a caller-stable `requestId`. Retrying that request ID returns the same attempt; it does not submit a duplicate task. Editing assignments require `sourceRepo` and a full `baseCommit`. Relay verifies the checkout against the configured agent's allowed roots and creates or reuses a separate, Relay-managed worktree. The source checkout is not edited by the dispatch operation. Research and review assignments use the configured read-only path. Only one attempt for an assignment may be active, including across assignment revisions. Record the worktree, branch, base revision, resulting commits, and review evidence when coordinating integration.

Execution records survive a Relay restart in local SQLite. Startup reconciliation checks task IDs and marks missing or uncertain submissions interrupted with replacement context; it never blindly replays edits. A replacement execution receives recorded assignment context and prior results, and is explicitly marked as not having resumed a native session. The current Relay task `sessionId` is a correlation ID, not a native harness session. Verify attempt status before retrying work.

## Discussions, inbox, and authority

Use `post_team_message` / `POST /v1/teams/{teamId}/messages` for an addressed message, question, proposal, challenge, request, or advice. Use `conclude_team_discussion` / `POST /v1/teams/{teamId}/conclusions` to preserve a summary, rationale, action, and whether the lead must decide. Setting `needsLead` also delivers the conclusion to the lead inbox. A peer message is advice or a request, not authority to change another assignment, priority, scope, permission, or success criterion. Workers route user decisions to the lead; the board has no worker-to-user reply control.

The owner can inspect run events and worker messages. A participant bearer credential is scoped to one run and participant. The owner creates one with `POST /v1/teams/{teamId}/credentials` and supplies it securely to that worker's process; it cannot list runs or manage assignments, phases, dispatches, or closure. A worker can read its run, post or conclude messages within its permissions, and read/acknowledge its addressed inbox. `GET /v1/teams/{teamId}/inbox?unreadOnly=true` lists unacknowledged deliveries; `POST /v1/teams/{teamId}/ack` acknowledges an event. Posting or recording a request does not prove it was delivered, read, or answered. Check the event thread and acknowledgement state. After the lead has dispatched an assignment once, a new addressed discussion message can automatically start a follow-up execution when that assignment is idle. The follow-up reuses verified dispatch provenance and recorded context, has a durable message-specific request ID, and obeys phase, dependency, concurrency, message, and elapsed-time limits. Acknowledging a delivery never starts work. The external lead checks its inbox with `get_team_inbox` or bounded `wait_team_messages`; Relay does not autonomously wake the lead harness.

Questions for the user are routed through the lead. The lead records them at `POST /v1/teams/{teamId}/questions` and records an answer at `POST /v1/teams/{teamId}/questions/{questionId}`. Independent work can continue while a decision is pending if it does not depend on that decision.

## Board and HTTP reference

All API calls use `Authorization: Bearer <token>`. Use the Relay owner token for lead operations and the run-scoped participant credential only for that participant's permitted operations.

- `GET /v1/teams` and `POST /v1/teams`: list runs and create a run (owner).
- `GET /v1/teams/{teamId}`: read the team view; participant views are scoped to their run.
- `POST /v1/teams/{teamId}/credentials`: create a participant credential (owner).
- `POST /v1/teams/{teamId}/assignments`, `/phase`, `/dispatch`, `/close`, `/summary`, `/artifacts`: lead operations.
- `POST /v1/teams/{teamId}/messages` and `/conclusions`: record peer or lead discussion.
- `GET /v1/teams/{teamId}/inbox`, `POST /v1/teams/{teamId}/ack`: read and acknowledge addressed deliveries.
- `POST /v1/teams/{teamId}/questions` and `/questions/{questionId}`: ask and resolve a user question through the lead.
- `GET /v1/teams/{teamId}/attempts`, `/summary`, `/questions`, and `/artifacts`: read those run records.
- `POST /v1/teams/{teamId}/history`: export selected records to local Git history.

The browser board displays the objective, phase, lead summary, roster, assignments, execution statuses, discussions, questions, artifacts, and limits. It refreshes recorded state; it is not a live worker monitor and a summary is not a complete conversation history. Execution status comes from recorded Relay task state, not from the presence of an assignment or message.

## Git history and completion

`POST /v1/teams/{teamId}/history` exports selected records to a dedicated local Git repository under Relay state. The export contains the run index, current draft or approved specification, recorded discussion conclusions, and closure milestones. It omits full message bodies, prompts, and credentials. The repository has no configured remote; export does not push. The response includes the commit and paths, and export failures are reported so the lead can retry. Git history is separate from the source-code worktrees.

Before closing, the lead presents artifacts, checks, review evidence, and remaining limitations, then calls `close_team` or `POST /v1/teams/{teamId}/close`. Closing blocks new autonomous dispatch; it does not claim that already-running work has stopped. Report in-flight attempts truthfully. This implementation does not provide native session continuation, reliable usage-cost accounting, or live paid-model acceptance testing; use explicit human review where evidence or adapter support is unavailable.
