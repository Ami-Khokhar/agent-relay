---
name: pr-gatekeeper-protocol
description: "Use when working in a repository that runs pr-gatekeeper and you are about to file an issue, start a task, open or update a pull request, or respond to a gatekeeper review (labels gatekeeper:*, comments containing '<!-- pr-gatekeeper', or outcomes MERGE/CHANGES/HUMAN). Defines the standard operating procedure: issue-first workflow, branch naming, the six-heading PR body, the review loop after CHANGES, the HUMAN stop rule, and the GITHUB_TOKEN re-trigger rule. For installing pr-gatekeeper into a repo, use the pr-gatekeeper-setup skill instead."
---

# pr-gatekeeper-protocol

The standard operating procedure for any coding agent working in a repository
guarded by pr-gatekeeper. Every PR is reviewed automatically and gets exactly
one decision per round: `MERGE` (bot squash-merges), `CHANGES` (fix and push),
or `HUMAN` (stop). The workflow is issue-first: no issue, no PR.

## The pipeline (one task = one issue = one branch = one PR)

```
issue → branch → small change + tests → run suite → PR (6 headings)
  → gatekeeper review → MERGE | CHANGES (loop, max 3) | HUMAN (stop)
```

## 1. File the issue first

- One task per issue. The issue title is the task; the body states the problem
  and the acceptance criteria (what must be true when it's done).
- Check for an existing issue before opening a new one.
- Link evidence: failing test output, error text, or the user's request.
- The PR will reference this issue (`Closes #N`), so write it as something a
  PR can close.

## 2. Branch and diff discipline

- Branch from the default branch; name it for the task
  (e.g. `fix/setup-feedback-6`). One branch per issue; no stacking unrelated
  work.
- Keep the diff within limits: **≤ 600 changed lines and ≤ 20 files**, or the
  repo's `.github/pr-gatekeeper.json` values. If a task is bigger, split it
  into multiple issues/PRs.
- Add or update tests that **fail on the old code and pass on the new code**.
- Run the repo's full test suite yourself and fix everything **before** pushing.
- Never put secrets in code, commit messages, or PR text.

## 3. Open the PR — the six-heading body (required)

The body must contain all six level-2 headings. A body that fails the format
lint (stage 0) wastes a review round.

| Heading | Rule |
|---|---|
| `## Task` | Issue reference: `#123`, `Closes #123`, or a URL |
| `## Intent` | ≥ 1 non-empty sentence: what this PR does |
| `## Why it was needed` | ≥ 1 sentence: the problem before |
| `## Why this approach` | ≥ 2 sentences: this design + rejected alternatives |
| `## What changed` | Bullet list; **every** changed/deleted path, exact set match with the diff |
| `## Proof of work` | ≥ 1 fenced block with a line starting `$ ` — real commands and results |

`## What changed` specifics:

- Path set must **exactly equal** the diff's changed paths — missing and extra
  paths are both violations.
- Format: `` - `src/app.py`: added retry logic `` · deleted: `` - `src/old.py` (deleted) ``.

`## Proof of work` specifics:

- The gates job **executes** the repo's real verify command; the reviewer job
  reads `gates.json` and never runs your code. So the proof must be commands
  you actually ran, with real output — it is cross-checked against gates.json.
- Use the repo's allowlisted commands (e.g. `python3 -m unittest ...`, `pytest`,
  `npm test`).

## 4. While the review runs

It triggers on open, every push, and body edits. Do **not** merge, and do not
push unrelated commits while it runs.

```bash
# poll (every ~60s, up to 30 min)
gh pr view <N> --json labels,state
# or watch the run
gh run watch $(gh run list --workflow pr-gatekeeper.yml --limit 1 --json databaseId -q '.[0].databaseId')
```

Outcomes (also the PR's single label):

- `gatekeeper:approved` (MERGE) — done; the bot squash-merged. Nothing to do.
- `gatekeeper:changes` (CHANGES) — fix and push (§5).
- `gatekeeper:needs-human` (HUMAN) — stop (§6).

## 5. The CHANGES loop

1. **Read the latest review** — the last PR comment whose body contains the
   marker `<!-- pr-gatekeeper `. Fetch with
   `gh pr view <N> --json comments`, scan newest → oldest, stop at the first
   with the marker.
2. **Parse the hidden JSON** on the marker line:
   `<!-- pr-gatekeeper {"round":2,"outcome":"CHANGES","head_sha":"...","findings":[{"severity","path","line","problem","fix"}]} -->`
   - JSON is between `<!-- pr-gatekeeper ` and ` -->`; `>` is escaped as
     `\u003e` (standard `json.loads` decodes it).
   - Only `severity: "blocking"` findings must be fixed; `minor` is advisory
     but fix when cheap.
   - If JSON doesn't parse, use the visible markdown above the marker.
3. **Fix in place**: same branch, same PR. Never open a new PR or branch for
   review fixes.
4. **Update the PR body**: keep all six headings; add every newly
   changed/deleted path to `## What changed`; refresh `## Proof of work` with
   the commands proving the fix.
5. **Push.** The review re-runs on push. If you pushed with the workflow's
   `GITHUB_TOKEN` inside Actions, the workflow will not self-trigger — end
   your run with `gh workflow run pr-gatekeeper.yml -f pr=<N>` (always safe
   even with a PAT).

**Budget: 3 rounds.** After 3 CHANGES rounds the next review is HUMAN by
design. Fix everything in the first pass.

## 6. HUMAN — hard stop

HUMAN means: protected paths touched, suspected secrets, too many rounds,
model disagreement, or a red base branch. Stop, tell the user the reasons from
the summary comment, and wait for their decision. Do not retry, do not push
"one more fix", do not work around the gatekeeper.

## Hard rules (never violate)

- Never edit `.github/` or `AGENTS.md` in a feature PR (protected; forces HUMAN).
- Never merge your own PR — only the gatekeeper merges.
- Never disable or bypass the gatekeeper.
- One task per issue and PR; small diffs; tests that prove the change.
- Secrets never in code, commits, or PR text.

## Automation hooks (for orchestrators)

- Completion event: `repository_dispatch` type `gatekeeper-review-completed`
  with payload `{pr, outcome, round, head_sha}` — drive follow-up agent runs
  without polling:
  ```yaml
  on:
    repository_dispatch:
      types: [gatekeeper-review-completed]
  ```
- Chain with agent-relay: on CHANGES, delegate the fix task to the same or
  another agent with `requestId` including the round number (e.g.
  `pr-123-fix-r2`) for idempotency.

## Reference

- Repo protocol block: `AGENTS.md` (`<!-- pr-gatekeeper:start -->` section) —
  authoritative for this repo's exact format and limits.
- Repo config: `.github/pr-gatekeeper.json` (verify command, limits, models).
- Installer SOP: the **pr-gatekeeper-setup** skill.
