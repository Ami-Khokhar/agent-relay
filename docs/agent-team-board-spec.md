# Agent Relay team collaboration board

Status: approved product specification; initial implementation locally verified.
Live harness validation remains outstanding.
Date: 2026-11-24.

## Purpose

Expand Agent Relay from task delegation into a workspace where a lead agent
coordinates a team of agents using different harnesses and models. Agents can
ask questions, debate approaches, review work, and record conclusions throughout
an objective. A frontend makes progress understandable without requiring the
user to read every internal exchange.

Vercel Board inspired the discussion. Integrating Vercel Board is not a requirement.

## Primary experience

The user continues talking to their existing lead agent in a harness such as
Claude Code or Codex. The lead creates a Relay team run for an objective, chooses
workers from the configured roster, assigns roles, and coordinates delivery.
The board accompanies that conversation; it does not host a replacement lead
chat in the first version.

The user primarily sees the objective, the lead's summary, assignments and
progress, questions requiring their decision, and resulting artifacts. Internal
agent conversations are available in a read-only side panel. The user steers
only the lead, including when reacting to something seen in that panel.

Example: the user asks their Claude Code lead to improve a product. The lead
clarifies the goal, demonstrates an approach, agrees the specification, and
assigns implementation and review to suitable configured workers. A reviewer
tags an implementer to discuss a concern. They compare evidence and record a
conclusion. The lead resolves scope questions and presents the integrated result.

## People, agents, and responsibilities

- User: supplies intent, judges prototypes, approves the specification and
  material changes to it, and answers questions brought by the lead.
- Lead: owns the objective, creates the team, assigns and revises work,
  coordinates integration, consolidates questions, and reports results.
- Worker: owns an assignment, initiates relevant questions, responds to tags,
  contributes evidence, and requests review or assistance from peers.
- Evaluator/reviewer: an assignment or role given to a worker, not a separate
  privileged class. Reviews should use a different perspective where useful.

Role, harness, and model are separate properties. The lead selects combinations
based on the work rather than assuming a model name implies a role.

## Discovery, prototype, and specification workflow

1. Explore: the lead interviews the user, researches facts, and records decisions.
2. Demonstrate: where useful, show the smallest prototype that tests the idea.
   A frontend may use an interactive mockup; a backend may use a sample API or
   command interaction. Explain when a prototype would add little evidence.
3. Agree: incorporate feedback into a specification with acceptance criteria
   and explicit unresolved questions. Obtain the user's agreement before
   launching implementation work. Research and prototypes may precede agreement.
4. Build: form the implementation team and execute assignments, peer discussions,
   review, integration, and verification.
5. Refine: compare the result with the agreed criteria and user feedback.
6. Complete: the lead presents artifacts, evaluation evidence, and remaining
   limitations. Close the run and stop autonomous team activity. A follow-up
   explicitly reopens work.

Prototype code is exploratory unless deliberately selected for production use.
Routine technical decisions belong to the lead and team. Material changes in
behavior, scope, agreed intent, or acceptance criteria return to the user through
the lead before dependent work proceeds.

## Discovering subjective intent

Use the user's `intent-to-evals` skill when the user can recognize or feel a
problem but cannot explain what makes it wrong. Do not invoke this process for
requirements that already have direct, reliable checks.

The lead offers small contrasting examples, asks which feels closer and why,
explains a hypothesis about the difference, and tests that understanding on fresh
examples. The user supplies taste; the lead discovers and validates the checks.
The user can reject both examples or combine them.

For each useful check, record its inputs, procedure, grading rule, failure
evidence, and any failures that outweigh a better score elsewhere. Verify that
the check distinguishes an accepted example from a plausible near miss, and
try to find an output it rewards that the user would reject. Keep distinct
preferences separate while their tradeoff remains unresolved.

Refine against the same baseline and checks under the same conditions. Report
improvements and regressions; account for normal scoring variation and confirm
with fresh cases. Where evaluation remains unreliable, retain human judgment as
the acceptance check. Changes to intent or criteria require user agreement and
re-evaluation of the baseline; workers cannot redefine success to pass.

Source: https://github.com/Ami-Khokhar/skills/blob/add-intent-to-evals/skills/intent-to-evals/SKILL.md

## Team formation

Provide a team-building skill for the lead. It must help the lead:

- Inspect the configured roster, supported capabilities, and available models.
- Decompose the objective into assignments and dependencies.
- Choose team size, roles, harnesses, and models within the run's limits.
- Use complementary expertise and independent review where it adds value.
- Explain the chosen team and avoid creating agents with no useful assignment.

The lead forms teams autonomously from configured workers. New harness setup,
new credentials, or expanded permissions are not implicit in team formation.
The skill guides choices; Relay enforces authority and resource limits.

## Communication and authority

Communication is threaded and associated with a team run and relevant assignment.
Participants can tag another participant, ask a question, propose an approach,
challenge a finding, or request assistance. A peer message is advice or a request,
not an order. Recipients can disagree and discuss alternatives before deciding
how and why to proceed.

Only the lead may intervene freely across discussions. Workers initiate messages
relevant to their assignment and respond when assigned work or explicitly tagged.
The delivery/context-selection mechanism remains an implementation decision;
it must support relevant context without requiring every worker to process every
message.

Peers may agree on implementation details within their assignments. Only the lead
changes assignments, priorities, scope, or permissions. Substantive discussions
record a conclusion and rationale, including who will act and whether a scope
change needs the lead. Unresolved disagreements escalate with alternatives,
evidence, and remaining uncertainty after a bounded discussion, or earlier when
blocked. The lead decides, requests an experiment, or asks the user.

Workers route questions for the user through the lead. The lead answers what it
can, combines overlapping questions, and asks only for decisions it cannot
reasonably make. Independent work can continue while a decision is pending.
There is no user reply control in worker threads; user steering targets the lead.

## Sessions and message delivery

Every participant has a stable identity within a run, distinct from its harness,
model, role, and individual execution attempt. Record addressed messages and
their delivery/response state so the board can distinguish a posted request from
an answer.

A tag after an assignment finishes should return the worker to its previous
context. Use native session continuation where supported. Otherwise create a
replacement session with the assignment and relevant recorded context, visibly
marking the replacement. Never imply native memory was retained when it was not.

Interrupted or disconnected executions must not be displayed as actively working
without checking their state. Recovery must avoid blindly repeating code changes
or other effects. Exact adapter contracts and recovery mechanics require a
technical design validated against each supported harness.

## Code collaboration

Use separate Git worktrees for agents editing code. Research and review workers
may inspect the relevant checkout under their configured permissions. Record
assignment, branch/worktree, base revision, resulting commits, and review evidence.
The lead coordinates integration and resolves conflicts. A peer's request cannot
silently transfer ownership of another assignment or its worktree.

## Board frontend

The main run view contains:

- Objective and phase: exploration, prototype, specification, build, or review.
- Lead-authored summary and the time of its latest update.
- Team roster with role, harness, model, assignment, and execution status.
- Assignments, dependencies, blockers, and progress.
- Questions or decisions awaiting the user, routed through the lead.
- Prototype, specification, evaluation, code, review, and final-result artifacts.
- Resource limits and available usage information, with unknown values explicit.

Selecting an assignment or participant opens a read-only conversation side panel
with messages, tags, evidence, conclusions, and session replacement indicators.
Steering remains in the existing lead conversation for the first version. Board
updates reflect recorded events; summaries must not masquerade as a complete
message history or as proof that an agent is live.

## Durability and Git history

Assignments, messages, decisions, results, and execution attempts survive a Relay
restart. Git provides retrievable, reviewable project history.

Recommended storage design: keep live coordination and recovery records in a
local transactional database, and version agreed specifications, decisions,
evaluation definitions, and milestone summaries in Git. Git entries reference
the relevant run and records. Do not create a commit for every agent message.
The user confirmed Git for history; the database/Git split is an engineering
recommendation, not a separately confirmed storage requirement. Full conversation
export and retention policy must be defined in the technical design.

Record authorship, timestamps, revisions, and links between decisions and work.
Do not automatically publish transcripts, prompts, or credentials to a remote
repository. Git export failures must be visible and recoverable without losing
the durable coordination history.

## Limits and stopping

Enforce configurable concurrency, elapsed-time, and spending limits where usage
is measurable. Display unavailable cost data as unknown rather than zero. A hard
monetary cap requires an adapter that supports reliable accounting/enforcement;
the UI must distinguish an estimate from an enforceable cap.

The lead asks before expanding limits. Apply bounded peer-discussion budgets and
prevent acknowledgments or duplicate delivery from causing endless exchanges.
On completion or a resource stop, cease new autonomous dispatch and represent
in-flight work truthfully. Exact defaults and stop/cancellation behavior are
technical-design decisions that must respect adapter capabilities.

## Acceptance

- Mixed teams collaborate — done when: an existing lead harness coordinates two
  configured workers with different harness/model combinations, and the board
  identifies their roles and assignments correctly.
- Peers reason together — done when: one worker tags another, they exchange a
  proposal and challenge, and a recorded conclusion reaches the lead.
- Authority is preserved — done when: a peer request cannot change another
  worker's assignment, scope, or permissions; the lead can revise assignments.
- User interaction stays with the lead — done when: worker questions are
  consolidated by the lead, independent work continues, and internal threads
  have no direct user reply control.
- Discovery precedes implementation — done when: a prototype and specification
  are reviewable before implementation dispatch, and material scope changes
  require a new user decision.
- Subjective feedback becomes useful evidence — done when: contrasting examples
  reveal a preference, checks are tried on fresh cases and near misses, and an
  unreliable evaluator remains under human review.
- Context continuation is honest — done when: a later mention resumes a supported
  native session or visibly starts a replacement with recorded context.
- Recovery is durable — done when: a restart preserves discussions and work
  records, interrupted executions are reconciled, and no work is blindly replayed.
- Git history is retrievable — done when: an agreed specification and milestone
  decision can be retrieved at recorded Git revisions and linked to their run.
- Parallel edits remain attributable — done when: two editing workers use
  separate worktrees and their reviewed contributions can be integrated.
- Limits and completion stop dispatch — done when: configured limits and a closed
  run prevent further autonomous work, with unsupported cost enforcement explicit.
- Existing delegation remains compatible — done when: existing single-task and
  workflow clients continue to work without creating or joining a team.

## Delivery outline and remaining design work

First validate a prototype of the main view and conversation panel. Then deliver
a narrow end-to-end slice: existing lead, two workers, addressed discussion,
recorded conclusion, and visible progress. Add recovery, Git records, limits,
and the discovery/evaluation workflow in independently reviewable increments.
Do not label the feature complete until the acceptance scenarios above pass.

Before implementation, resolve adapter continuation/wake capabilities; local
browser authentication and read permissions; database schema and migration;
event ordering, deduplication and delivery acknowledgment; retention and export;
Git location and commit policy; limit defaults and cancellation semantics; and
integration with existing artifact work. The specific frontend framework, wire
protocol, and database schema are not selected by this product specification.

This specification authorizes no deployment, service restart, automatic merge,
new external communication integration, or changes to existing review controls.
