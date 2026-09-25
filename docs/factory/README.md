# Factory contracts (Phase 0)

The frozen interfaces of the software factory. Cells exchange **documents, never
vendor SDKs** — that is what keeps the line harness- and model-agnostic. These
four documents are the only things that cross a boundary.

Status: **frozen v1**. These are the contract the controller is built against.
They currently live in this repo as a staging area; they move to the
`factory-controller` repo when it is created (Phase 1).

See also: [`docs/factory-architecture.html`](../factory-architecture.html) ·
[`decisions.md`](decisions.md).

## The four documents

| Document | Direction | Producer | Consumer | Schema |
|---|---|---|---|---|
| **WorkItem** | internal (durable) | controller | controller | [`schemas/workitem.schema.json`](schemas/workitem.schema.json) |
| **WorkOrder** | controller → worker | controller | worker (via agent-relay) | [`schemas/workorder.schema.json`](schemas/workorder.schema.json) |
| **Result** | worker → controller | worker | controller | [`schemas/result.schema.json`](schemas/result.schema.json) |
| **Verdict** | gate → controller | pr-gatekeeper | controller | [`schemas/verdict.schema.json`](schemas/verdict.schema.json) |

```
            ┌───────────────┐
            │   WorkItem    │  one durable record per deliverable issue
            │   (durable)   │  controller-owned, reconciled from GitHub facts
            └───────┬───────┘
        WorkOrder   │   ▲   Result
        (to worker) │   │   (artifact handoff)
                    ▼   │
            ┌───────────────┐
            │    Worker     │  any harness behind the agent-relay adapter
            └───────────────┘
                    ▲
        Verdict     │   (normalized gate decision)
        (from gate) │
            ┌───────────────┐
            │ pr-gatekeeper │
            └───────────────┘
```

## Design rules baked into the schemas

- **The artifact is the handoff.** A `Result` with `status: completed` **must**
  carry `artifact` (branch + commit) and evidence. Free-text `notes` are for
  humans; the control path must never parse them. This is what makes any
  harness that can commit a first-class worker.
- **`Verdict.headSha` is mandatory.** A verdict only applies to the exact commit
  it names. Acting on an older SHA is the stale-review bug, so it is
  structurally rejectable.
- **`WorkItem.stateVersion` is optimistic concurrency.** Every durable write
  increments it; a transition that writes on a stale version loses.
- **`human_waiting` is not terminal.** `merged`, `closed`, and `cancelled` are.
  HUMAN is an action queue with a path back.
- **`blockers[].fingerprint` is the identity of "the same problem."** Round
  counting is per unresolved problem, not per gate round (see
  [decisions.md](decisions.md), ADR-0005).
- **`WorkItem.gateRound` mirrors pr-gatekeeper's round number** and is never
  incremented by controller retries.
- **Unknown properties are rejected** (`additionalProperties: false`) so a cell
  cannot smuggle vendor-specific fields across a boundary.

## State machine

```
intent → issued → planned → ready → dispatching → implementing → pr_open
                                     ▲                                │
                                     │                                ▼
                              changes_requested ◀────────────── reviewing
                                     │                                │
                                     │                                ▼
                                     │                          merge_pending
                                     │                                │
                                     │                                ▼
                          human_waiting ──► (ready | closed)     merged → closed
```

Operational fault states: `blocked`, `failed`.
Terminal states: `merged`, `closed`, `cancelled`. **`human_waiting` is not
terminal.**

## Controller obligations (the guards)

These are correctness rules, not features. They come directly from the review and
must hold before the controller is allowed to write:

1. **Reconcile, don't trust events.** GitHub is authoritative for facts;
   `repository_dispatch` is a wake-up hint. A periodic reconcile is the truth.
2. **Guard every transition.** Fetch the PR and confirm
   `repo + prNumber + headSha + round` match the WorkItem. Ignore stale or
   duplicate verdicts.
3. **Single writer.** Acquire a per-WorkItem lease before dispatching; at most
   one active WorkOrder per branch.
4. **Idempotent dispatch.** Every WorkOrder has a stable `idempotencyKey`. On a
   relay timeout, inspect the branch/commit/PR before retrying.
5. **`merge_pending` before `merged`.** Only observe `merged` after GitHub
   confirms the merge on the reviewed SHA; running a merge and observing a merge
   are different facts.
6. **Acceptance, not just merge.** `closed` requires the issue's acceptance
   criteria to be checked, not merely a merged PR.

## Versioning

- The `schemaVersion` const is the version. Additive, backward-compatible
  changes keep `v1`; any breaking change becomes `v2` and both may coexist
  during migration.
- A consumer must reject a document with an unknown `schemaVersion` rather than
  guess.

## Phase 0 registry single-source-of-truth

The agent registry is machine state, and it now has one home:
**`~/.config/agent-relay/agents.json`** (the relay's resolution order already
prefers it). Before this, the repo checkout and the installed clone each had
their own `config/agents.json`, and the relay silently fell back to whichever
ran — different worker pools depending on cwd.

- Canonical file: `~/.config/agent-relay/agents.json` (7 agents).
- Both checkout fallbacks are aligned to it; originals kept as
  `config/agents.json.bak-phase0`.
- Proof: both checkouts report the same `/v1/agents` and log the same registry
  path.

Follow-up (not Phase 0): the silent fallback in `default_config_path()` is the
drift generator — it should warn or fail loud when the user registry is absent.
That is a normal agent-relay change, and a good first dogfood PR.
