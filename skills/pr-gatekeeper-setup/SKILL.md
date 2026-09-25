---
name: pr-gatekeeper-setup
description: "Use when asked to install, set up, or wire pr-gatekeeper into a repository (greenfield or existing), when a target repo has no .github/workflows/pr-gatekeeper.yml, or when pr-gatekeeper workflow logic must stay private. Covers the reusable-workflow wiring, the public wrapper repo pattern, repo config (.github/pr-gatekeeper.json), secrets, the PR template, AGENTS.md, and first-run verification. For how to behave once installed (issues, branches, PRs, review loop), use the pr-gatekeeper-protocol skill."
---

# pr-gatekeeper-setup

Install pr-gatekeeper into a target repository so every pull request is
automatically reviewed and returns one decision: `MERGE`, `CHANGES`, or
`HUMAN`. This skill is the installer SOP. The day-to-day agent behavior
(issues, branches, PR bodies, the review loop) lives in the
**pr-gatekeeper-protocol** skill — do not duplicate it here.

## What gets installed (5 files, one decision)

| # | File | Purpose |
|---|---|---|
| 1 | `.github/workflows/pr-gatekeeper.yml` | Caller workflow: triggers the review |
| 2 | `.github/pr-gatekeeper.json` | Repo config: verify command, limits, models |
| 3 | `.github/pull_request_template.md` | Six-heading PR body template |
| 4 | `AGENTS.md` | The agent protocol block (from pr-gatekeeper-protocol) |
| 5 | Secrets | `OPENCODE_GO_API_KEY` (and `GATEKEEPER_READ_TOKEN` only if needed) |

Plus **one architectural decision**: where the reusable review workflow lives.

## Decision: private logic vs public wrapper

GitHub rule you are working around: a caller workflow can reference a reusable
workflow (`uses: owner/repo/.github/workflows/x.yml@ref`) only if that workflow
is **accessible to the caller**. A private `pr-gatekeeper` repo is not readable
by an unrelated repo's `GITHUB_TOKEN`, so the `uses:` reference fails.

| Option | When to choose | Trade-off |
|---|---|---|
| **A. Public wrapper repo** (recommended) | The reusable `gatekeeper.yml` contains review logic you do not want to publish | Logic stays private; one tiny public repo to maintain; `uses:` points at the wrapper |
| **B. Make pr-gatekeeper public** | Nothing in the workflows is sensitive | Simplest wiring, zero extra repos |
| **C. Same-org + fine-grained PAT** | Wrapper repo is not acceptable, everything in one org | PAT must be rotated; org setting "accessible from repositories in the organization" must allow it |

### Option A — public wrapper repo (the recommended path)

The wrapper repo contains **only** a thin `workflow_call` shim. The heavy logic
stays private; the wrapper checks out the private repo at run time using
`GATEKEEPER_READ_TOKEN` (a fine-grained PAT with **Contents: read-only** on the
private repo only).

Wrapper repo layout (public, e.g. `Ami-Khokhar/gatekeeper-entry`):

```
.github/workflows/gatekeeper.yml
```

The wrapper's `gatekeeper.yml` declares `on: workflow_call` with the same
inputs/secrets the real workflow expects (`pr`, `base_sha`, `gatekeeper_ref`,
`OPENCODE_GO_API_KEY`, `GATEKEEPER_READ_TOKEN`), then:

```yaml
steps:
  - uses: actions/checkout@v4
    with:
      repository: Ami-Khokhar/pr-gatekeeper   # the PRIVATE repo
      ref: ${{ inputs.gatekeeper_ref }}
      token: ${{ secrets.GATEKEEPER_READ_TOKEN }}  # PAT with read access to it
      path: gatekeeper
      persist-credentials: false
  # then run the real review logic from ./gatekeeper
```

`persist-credentials: false` so the PAT never leaks into later steps of user
PR code. Keep the wrapper's inputs/secrets names **identical** to the real
workflow's so target repos don't care which one they call.

Target repos then reference the wrapper:

```yaml
uses: Ami-Khokhar/gatekeeper-entry/.github/workflows/gatekeeper.yml@v1
```

## Step-by-step install (greenfield repo)

Run all steps; do not skip verification.

1. **Check the current state.** Does `.github/workflows/pr-gatekeeper.yml`
   already exist? If yes, this is an update — diff against the reference and
   stop after step 5. Ask the user before overwriting any of the 5 files.

2. **Choose the wiring** (A/B/C above). Default to A when pr-gatekeeper is
   private and no wrapper exists yet; create the wrapper repo first (user must
   create it and add the PAT secret `GATEKEEPER_READ_TOKEN` to it).

3. **Write `.github/workflows/pr-gatekeeper.yml`** — the caller. It needs:
   - `on:` `pull_request` (`opened, synchronize, reopened, edited,
     ready_for_review`) + `workflow_dispatch` with a `pr` input.
   - `permissions:` `contents: write`, `pull-requests: write`, `issues: write`,
     `checks: write`, `actions: write`.
   - `concurrency:` group `pr-gatekeeper-<PR number>`, `cancel-in-progress: true`.
   - A `resolve` job (permissions `pull-requests: read` only) that resolves
     `base_sha` and skips drafts / dependabot / renovate / fork PRs.
   - The `gatekeeper` job that `uses:` the wrapper (or public repo) and passes
     `pr`, `base_sha`, `gatekeeper_ref`, and the two secrets.
   - Reference implementation: copy from the pr-gatekeeper repo's own caller
     workflow; do not re-derive it from memory.

4. **Write `.github/pr-gatekeeper.json`** — repo config. Ask the user (or
   detect from the repo) for:
   - `verify` — the full test-suite command (e.g.
     `python3 -m unittest discover -s test -p 'test_*.py'`, `npm test`,
     `uv run pytest`).
   - `test_file_globs` + `test_command` — for targeted re-runs.
   - `run_allowlist` — commands the gates may execute.
   - `max_changed_lines` (600), `max_changed_files` (20), `max_rounds` (3).
   - `protected_paths` — always include `.github/` and `AGENTS.md`.
   - `reviewer_model` / `verifier_model`, `merge_method` (`squash`), `notify`.
   Never invent a `verify` command: read the repo's test layout first
   (pyproject, package.json, Makefile) and confirm it runs locally.

5. **Write `.github/pull_request_template.md`** — six headings with HTML
   comments explaining each rule (Task / Intent / Why it was needed / Why this
   approach / What changed / Proof of work). Copy verbatim from pr-gatekeeper.

6. **Write `AGENTS.md`** — insert the `<!-- pr-gatekeeper:start -->` ...
   `<!-- pr-gatekeeper:end -->` protocol block from pr-gatekeeper. If
   `AGENTS.md` exists, append/replace only that block; do not clobber other
   content.

7. **Secrets.** Tell the user exactly which secrets to add in target repo
   Settings → Secrets and variables → Actions:
   - `OPENCODE_GO_API_KEY` — always.
   - `GATEKEEPER_READ_TOKEN` — only when wiring option A or C (fine-grained
     PAT, Contents: read-only, scoped to the private pr-gatekeeper repo).
   Agents must never write secrets anywhere — hand this list to the user.

8. **Verify the install.**
   ```bash
   # YAML parses and the uses: reference resolves:
   python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/pr-gatekeeper.yml'))"
   gh workflow list | grep pr-gatekeeper
   # JSON config parses:
   python3 -c "import json; json.load(open('.github/pr-gatekeeper.json'))"
   ```
   Then ask the user to open (or let you open) a **test PR** that changes one
   trivial file and watch:
   ```bash
   gh run watch $(gh run list --workflow pr-gatekeeper.yml --limit 1 --json databaseId -q '.[0].databaseId')
   gh pr view <N> --json labels,state   # expect gatekeeper:approved on a clean PR
   ```
   A green first round is the definition of a correct install.

## Common failure modes

| Symptom | Cause / fix |
|---|---|
| `workflow is not reusable as it is missing a on: workflow_call trigger` | The `uses:` target doesn't declare `workflow_call` — you referenced the wrong file/version. |
| `Resource not accessible by integration` on the `uses:` line | Private repo not accessible to the caller → wiring option A with `GATEKEEPER_READ_TOKEN`, or make it public. |
| Review runs but fails with no findings | `OPENCODE_GO_API_KEY` missing in the **target** repo, or the secret name in `secrets:` doesn't match. |
| Fork/Dependabot PRs show skipped runs | Expected: the skip rule intentionally skips PRs that would have no secrets. |
| Two reviews fighting on one PR | `concurrency` group missing or not keyed on the PR number. |
| Gates pass but reviewer can't see the diff | `base_sha` not resolved/passed by the caller's `resolve` job. |

## Handoff

After a verified install, tell the user that day-to-day agent behavior
(issues → branch → PR → review loop → merge) is defined by the
**pr-gatekeeper-protocol** skill, which the PR template and `AGENTS.md` now
point at.
