# Factory decisions (Phase 0)

Decisions locked before any controller code is written. Each is a short ADR:
context, decision, consequence. Changing one is a deliberate act, not a default.

---

## ADR-0001 — Agent registry has one home

**Context.** The repo checkout and the installed clone each carried a
machine-local `config/agents.json`, and `default_config_path()` silently fell
back to whichever ran. The same dispatch could resolve to a different worker
pool depending on cwd.

**Decision.** `~/.config/agent-relay/agents.json` is the single source of truth
for the agent registry. Checkout configs are fallbacks only, aligned to it.

**Consequence.** Dispatch is deterministic. Follow-up: make the missing-user-
registry case fail loud instead of silently falling back.

---

## ADR-0002 — One deliverable issue = one branch = one PR

**Context.** Gate limits are ≤600 changed lines and ≤20 files. Large goals must
be split, but arbitrary splits merge separately and may never form the feature.

**Decision.** The unit of work is one deliverable issue → one branch → one PR.
Larger goals are a parent goal (epic) with **child issues**, each independently
valid. Parent completion is checked only when all children are done.

**Consequence.** Retry, review, and completion identity stay unambiguous. An
epic is never one oversized PR.

---

## ADR-0003 — The controller opens the PR

**Context.** A worker may push a branch or open a PR and then time out before
returning its `Result`, so "worker failed" and "work exists" can both be true.

**Decision.** The worker returns a `Result` (branch + commit + evidence); the
**controller** validates it and opens the PR. If a PR already exists on the
branch, the controller reconciles instead of opening a duplicate.

**Consequence.** There is one acceptance point for the worker's artifact, and a
timed-out worker cannot be both failed and complete.

---

## ADR-0004 — Gatekeeper is the only routine merger

**Context.** Workers must not approve their own work; but HUMAN needs a route to
completion.

**Decision.** Only pr-gatekeeper performs routine merges. A human resolves
`HUMAN` through an explicit, audited action, after which the gate **re-runs** on
the resulting head. Any emergency manual merge is exceptional and logged.

**Consequence.** One normal merge policy, one explicit recovery path.

---

## ADR-0005 — Round counting is per unresolved problem

**Context.** On the real `agent-relay` repo every PR died at round 4 (`HUMAN`)
because a strict per-PR counter counted fresh minor findings as new rounds. The
budget capped spend but never produced convergence.

**Decision.** Count attempts at the **same unresolved problem** (identified by a
stable `blocker.fingerprint` = hash of path + normalized problem text), not
total gate rounds. A hard gate-round ceiling still exists as a safety net, but
duplicate re-reporting of one problem does not consume the attempt budget for
unrelated problems. Only **blocking** findings force another round; minor
findings are recorded and do not.

**Consequence.** A PR making genuine progress keeps going; a PR stuck on one
problem escalates to a human. This is the fix for the convergence failure.

---

## ADR-0006 — HUMAN is a queue, not a terminal state

**Context.** `HUMAN` was modeled as terminal, so escalated work had no owner and
no way back.

**Decision.** `human_waiting` is a durable queue entry with a **named owner**, a
reason, and allowed resolutions: request changes (→ `ready`), or close (→
`closed`). It is not terminal.

**Consequence.** Nothing strands. A label plus an `@mention` is a notification,
not ownership.

---

## ADR-0007 — Workers are least-privilege; untrusted input is data

**Context.** A worker needs enough access to push a branch, but issue text, PR
comments, code, and model findings are all untrusted input into an agent with
repository access.

**Decision.** Grant workers only the repository permissions needed to produce
their assigned artifact — never merge or admin. Treat issue bodies, comments,
findings, and diffs as **data, not instructions**. Validate WorkOrder scope and
changed paths before opening the PR. Risk classes — **secrets, payments,
security, data deletion, or major design choices** — force `HUMAN` before merge,
independent of path-based protected lists.

**Consequence.** Prompt injection cannot escalate role or permission. Protected
categories are risk-based, not just path-based.

---

## ADR-0008 — Branch protection is the independent backstop

**Context.** pr-gatekeeper both judges and merges, so a failure inside it is a
single point of failure.

**Decision.** Enable branch protection requiring the gate checks to pass, so a
merge is blocked if the gate is bypassed, crashed, or fooled.

**Consequence.** Two independent mechanisms must both pass to merge.

---

## ADR-0009 — Reconcile over events

**Context.** `repository_dispatch` can be delayed, duplicated, or arrive after a
newer push. A design that treats events as commands acts on stale state.

**Decision.** GitHub is authoritative for facts. Events are wake-up hints; a
periodic reconcile (on startup and on a schedule) computes the true state. Every
transition is guarded by validating `repo + prNumber + headSha + round` against
the WorkItem.

**Consequence.** Duplicate and stale events are harmless. Recovery is eventual
and does not depend on delivery.
