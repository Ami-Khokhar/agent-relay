# Software Factory — Implementation Handoff

**Objective:** turn the existing review/merge cell (pr-gatekeeper) and multi-agent
execution bus (agent-relay) into a working software factory whose **control plane is
OpenBot**, so that a human states a goal and the system drives it to a reviewed,
merged PR, escalating to a human only on true exceptions.

**Status:** design frozen through Phase 0; OpenBot selected as the control plane;
implementation not started.

**Audience:** the implementing agent (or human) who picks this up next. Everything
needed to start is in this document plus the artifacts it links.

---

## 1. Locked decisions

Do not relitigate these; if one is wrong, stop and report it.

| # | Decision | Ref |
|---|---|---|
| D1 | One deliverable issue = one branch = one PR; epics are a parent goal + child issues | ADR-0002 |
| D2 | The **controller opens the PR** after validating the worker artifact | ADR-0003 |
| D3 | **pr-gatekeeper is the only routine merger**; humans resolve HUMAN then re-gate | ADR-0004 |
| D4 | Round counting is **per unresolved problem**, not per gate round | ADR-0005 |
| D5 | **HUMAN is a queue, not a terminal state**; it has an owner and a path back | ADR-0006 |
| D6 | Workers are least-privilege; issue text/findings are **data, not instructions** | ADR-0007 |
| D7 | Branch protection requires gate checks — independent backstop | ADR-0008 |
| D8 | **Reconcile over events**: GitHub is facts; events are wake-up hints | ADR-0009 |
| D9 | Agent registry has one home: `~/.config/agent-relay/agents.json` | ADR-0001 |
| **D10** | **OpenBot is the control plane**, but control decisions and merge authority stay deterministic/policy-enforced (not model-decided) | this doc |
| **D11** | **No Anthropic dependency** — the Claude subscription ends 2026-09-28; everything must run on OpenCode Go / OpenAI / local providers | this doc |
| **D12** | Providers are swappable and non-load-bearing; the router degrades on provider failure | this doc |

---

## 2. Current state (Phase 0 — done)

| Artifact | Path | State |
|---|---|---|
| Four frozen contracts | `a2a-relay/docs/factory/schemas/{workitem,workorder,result,verdict}.schema.json` | Validated (meta-schema + instances + negative test) |
| Contracts overview | `a2a-relay/docs/factory/README.md` | Written |
| Nine ADRs | `a2a-relay/docs/factory/decisions.md` | Written |
| Architecture diagram | `a2a-relay/docs/factory-architecture.html` | Written (needs an OpenBot update) |
| Registry SSOT | `~/.config/agent-relay/agents.json` | Established; both checkouts aligned; verified |

**Registry (canonical):** `codex`, `opencode`, `pi`, `deepseek-harness`,
`deepseek-harness-flowy`, `pi-glm`, `pi-deepseek`. No `claude`. No Anthropic.

**Known follow-up:** `default_config_path()` in agent-relay silently falls back to a
checkout when the user registry is absent — make it warn/fail loud. Good first dogfood PR.

### Pending contract edits (do these first)

These fold in OpenBot's job semantics and the hosted-provider handoff. Nothing consumes
the schemas yet, so they are cheap:

1. `WorkItem.acceptance` → cada item carries a checkable **`doneWhen`**, read back from
   the produced artifact, not from effort.
2. `WorkItem` blocked → require **`blockedOn`** plus **`attempts[]` (≥2)**, mirroring
   OpenBot's `job_blocked`.
3. Add terminal state **`exhausted`** (budget hit, neither done nor blocked).
4. `WorkOrder.budget` → two ceilings: **`maxPasses`** and **`wallMs`**.
5. `Result` handoff → allow **`artifact {branch, commitSha}` OR `patch {format, diff}`**
   (hosted providers like Codex Cloud return a diff).
6. Generalize `WorkItem.relayTaskId` → **`provider` + `providerTaskId` + `sessionHandle`
   + `sessionNative` + `sessionUrl`**.

---

## 3. Target architecture

```
                              HUMAN
                                │ intent · approvals (OpenBot app / Telegram)
                                ▼
        ┌──────────────────────────────────────────────────────────────┐
        │  OpenBot control plane                                         │
        │   Jobs (WorkItem)      Foreman coworker      Policy (CEL)      │
        │   Governance + audit   Planner coworker      Routines           │
        │   Postgres + memory    Channels / HITL       Computers          │
        └──────┬─────────────────────┬───────────────────────┬─────────┘
      MCP/tool │            governed │              event    │
               ▼                     ▼              bridge   ▼
        agent-relay (MCP)      GitHub connector     pr-gatekeeper verdicts
               │                     │                       │
               ▼                     ▼                       ▼
        workers pi/codex …    issues · PRs · commits      gates + merge
                                 (facts)
```

- **OpenBot** = control plane: durability, governance/audit, HITL, workspace computers,
  memory, routines, UI.
- **agent-relay** = governed tool (its MCP server: `list_agents`, `delegate`, `wait_task`,
  `get_task`, `list_tasks`, `cancel_task`).
- **pr-gatekeeper** = external gate + merge authority; unchanged.
- **GitHub** = source of truth for facts.

---

## 4. OpenBot control-plane mapping

| OpenBot | Factory |
|---|---|
| **Job** (`doneWhen`, passes/hours, `exhausted`) | **WorkItem** |
| `job_done` | acceptance verified → PR merged → closed |
| `job_blocked` (needs what you must supply + ≥2 attempts) | **HUMAN escalation** |
| `job_checkpoint` | working memory across reconcile passes |
| **Governance gateway** (CEL, audit rows, fail-closed) | worker authority, protected paths, no-merge |
| **Coworkers** (AG-UI) | **Foreman** (control), **Planner** (intake) |
| **Computers** (container + workspace) | worker workspace |
| **Channels** (app, Telegram) | escalation + ops console |
| **Routines** | periodic reconcile |
| **Postgres + Intelligence** | durable WorkItem state + memory |
| **MCP servers** | agent-relay + GitHub |

### Tenant package to build

OpenBot expects a tenant package under `examples/`. Ours is `examples/factory/`
(copy the shape of `examples/personal/`):

```
examples/factory/
  agents.yaml        # Foreman (deterministic core) + Planner
  model.yaml         # model-agnostic; points at the plan shim / OpenAI / OpenCode Go
  brand.yaml
  channels.yaml      # #factory + Telegram escalation
  skills.yaml
  knowledge.yaml
  agents/            # AG-UI endpoints (Foreman tools, Planner)
```

Policy is configured via `AGENT_COMPUTER_POLICY` / admin settings (not the package).
Factory rules to encode (CEL, fail-closed):

- deny `merge` from any Bot (gatekeeper merges);
- deny writes under `.github/`, `AGENTS.md`;
- deny secret-looking tool inputs / credential reads;
- deny commands outside the allowlist;
- deny cloud/remote egress for risk-classed repos.

---

## 5. Non-negotiables

1. **Determinism where it matters.** The Foreman's control actions are deterministic
   tools (`factory_reconcile`, `factory_dispatch`, `factory_apply_verdict`,
   `factory_escalate`, `factory_close`). The LLM layer does intake/planning and
   `job_blocked` reasoning only. It cannot force a merge or bypass a guard.
2. **Merge authority is pr-gatekeeper's.** Denied by policy for every Bot.
3. **Reconcile, don't trust events.** Validate `repo + prNumber + headSha + round`
   against the WorkItem before any transition.
4. **Async dispatch.** OpenBot caps a pass at 15 min (`JOB_PASS_TIMEOUT_MS`). Any longer
   coding task is `delegate` → `job_checkpoint` → reconcile next pass. Never block a pass.
5. **No Anthropic dependency** (D11). Models via OpenCode Go / OpenAI / local.
6. **Agnostic + swappable providers** (D12); route on locality/risk; degrade on failure.
7. **Dogfood.** Every factory change goes through issue → branch → PR → gatekeeper.
   Never edit `.github/` or `AGENTS.md` in a feature PR.

---

## 6. Plan

| Phase | Build | Definition of done |
|---|---|---|
| **A · Stand up** | `examples/factory/` tenant package: Foreman + Planner, `agents.yaml`, policy, agent-relay MCP connector, GitHub connector, channels | A trivial job runs end-to-end and every tool call is audited |
| **B · WorkItem ⇄ Job** | Job payload + Postgres fields (repo, issue, branch, pr, headSha, round, blockers, provider); reconcile routine; GitHub→OpenBot event bridge | A GitHub issue becomes a durable job that survives an OpenBot restart |
| **C · Foreman** | Deterministic control tools + state machine; guards; async dispatch + checkpoint | One issue goes `ready → PR` with no human; a duplicate event is ignored |
| **D · Workers** | Policy-routed delegation: agent-relay MCP (local) + OpenBot computer (isolated); `artifact`/`patch` handling | Two different harnesses complete the same WorkOrder |
| **E · Gate loop** | Verdict ingestion (marker/labels/`repository_dispatch`); `CHANGES → fix`; per-problem rounds; `MERGE → close` | One real PR reaches `MERGED` with one auto-fix round |
| **F · HITL + ops** | `job_blocked` → channel/Telegram → approval → resume; cost ledger; **autonomy-rate metric** | A blocked job is resolved from Telegram and resumes |

**Project done test:** on a real repo, ≥1 issue completes `ready → merged` with zero human
intervention, including one automatic CHANGES→fix round, and the autonomy rate is logged.

---

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| OpenBot is **alpha** | Keep decisions in deterministic tools/policy so a platform failure stalls a job, never causes a wrong merge |
| Heavy footprint (Docker/Postgres/Intelligence/model key) | Accept for the control plane; keep workers lightweight; document the local launcher |
| 15-min pass cap | Async dispatch + checkpoint (Non-negotiable 4) |
| LLM Foreman drift / prompt injection | Deterministic tools; policy fail-closed; issue text is data |
| Claude subscription ends 2026-09-28 | D11: no Anthropic dependencies anywhere |
| Timeout → duplicate workers | Per-WorkItem lease + idempotency key; reconcile artifacts before retry |
| Stale verdict acted on | Guard on `repo+pr+headSha+round` |
| Relay task map lost on restart | Durable state lives in OpenBot/Postgres, not the relay |

---

## 8. Open questions

1. Does the OpenBot GitHub connector work as MCP, or via `gh` in a computer? Pick one and
   govern it.
2. Event bridge direction: GitHub `repository_dispatch` → HTTP into OpenBot, or an
   OpenBot routine that polls? (Recommend both: event for latency, routine for truth.)
3. Where does the Foreman run — a new AG-UI endpoint service, or an OpenBot computer?
4. Does the Planner emit the issue directly, or propose it to a human first for
   high-ambiguity goals?
5. Which risk classes force HUMAN on day one (recommend: secrets, payments, security,
   data deletion, `.github/`, `AGENTS.md`)?
6. Confirm OpenBot can call agent-relay MCP with a bounded `wait_task` that never exceeds
   the pass timeout (use `delegate` + poll, not `wait_task`).

---

## 9. Immediate first steps

1. **Apply the six contract edits** (§2) to `a2a-relay/docs/factory/`, re-validate the
   schemas, and add **ADR-0010: OpenBot is the control plane; decisions and merge stay
   deterministic** plus **ADR-0011: no Anthropic dependency**.
2. **Scaffold `examples/factory/`** in the OpenBot clone (copy `examples/personal/` shape).
3. **Wire agent-relay's MCP server** (`a2a-relay/src/mcp_server.py`) into OpenBot; prove
   `delegate` / `wait_task` / `cancel_task` appear as **governed, audited** tools.
4. **Add the factory policy** via `AGENT_COMPUTER_POLICY` and prove a denied merge is
   refused and audited.
5. **Build the WorkItem ⇄ Job payload** and a single reconcile routine.
6. **Prove one end-to-end job** through the Foreman, then open the first real issue
   through the factory itself.

---

## 10. Artifact index

| What | Where |
|---|---|
| Contracts | `a2a-relay/docs/factory/schemas/*.schema.json` |
| Contracts overview | `a2a-relay/docs/factory/README.md` |
| ADRs | `a2a-relay/docs/factory/decisions.md` |
| Architecture | `a2a-relay/docs/factory-architecture.html` |
| Agent registry | `~/.config/agent-relay/agents.json` |
| Relay source | `a2a-relay/src/server.py`, `src/mcp_server.py` |
| Gate source | `pr-gatekeeper/gatekeeper/`, `pr-gatekeeper/.github/workflows/gatekeeper.yml` |
| Gate config | `a2a-relay/.github/pr-gatekeeper.json` |
| OpenBot clone | `~/Documents/Coding projects/openbot` (CopilotKit/openbot) |
| OpenBot docs | `openbot/docs/{architecture,jobs,coworkers,routines,configuration}.md` |
| OpenBot tenant examples | `openbot/examples/{personal,fintech}/` |
| Local launcher | `~/.local/bin/openbot` (plan shim on port 4300) |

**Useful commands**

```bash
# relay health + agents
curl -sS http://127.0.0.1:43124/healthz
curl -sS http://127.0.0.1:43124/v1/agents

# validate the contracts
cd a2a-relay && python3 - <<'PY'
import json,glob
from jsonschema import Draft202012Validator as V
for f in sorted(glob.glob('docs/factory/schemas/*.schema.json')):
    V.check_schema(json.load(open(f))); print("OK", f)
PY
```

---

## 11. Glossary

- **WorkItem** — one deliverable issue's durable orchestration record.
- **WorkOrder** — the instruction handed to a worker for one execution.
- **Result** — a worker's artifact/patch + evidence handoff.
- **Verdict** — a normalized, forge-neutral gate decision (MERGE/CHANGES/HUMAN).
- **Foreman** — the deterministic control coworker in OpenBot.
- **Job** — OpenBot's goal-until-finished primitive, used as the WorkItem.
- **Pass** — one OpenBot job iteration (≤15 min), ended by done/blocked/exhausted.
- **Blocked** — HUMAN escalation with evidence (≥2 attempts).
- **Exhausted** — budget ceiling reached; distinct from done and blocked.
- **Autonomy rate** — fraction of work items reaching `merged` with no human touch.
