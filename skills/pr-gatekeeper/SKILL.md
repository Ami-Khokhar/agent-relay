---
name: pr-gatekeeper
description: "Index skill for pr-gatekeeper. Use when a PR has a gatekeeper:* label or a comment containing '<!-- pr-gatekeeper', when asked to install pr-gatekeeper into a repository, or when working in a gatekeeper-guarded repo and unsure which sub-skill applies. Routes to pr-gatekeeper-setup (install/configure) and pr-gatekeeper-protocol (issues, branches, PRs, review loop)."
---

# pr-gatekeeper (index)

A bot that reviews every pull request in a guarded repository and returns one
decision per round: `MERGE`, `CHANGES`, or `HUMAN`. It reads the PR body,
runs deterministic checks and a model review, and publishes the decision as a
label, a summary comment, a review, and a check run. It works with any coding
agent; no harness is required.

This is an index. Two sub-skills carry the actual instructions — load the one
that matches what you are doing:

## Which skill do I need?

| Situation | Load |
|---|---|
| "Set up / install / wire gatekeeper in this repo", greenfield or existing | **pr-gatekeeper-setup** |
| pr-gatekeeper workflow must stay private, need the public wrapper repo pattern | **pr-gatekeeper-setup** (Option A) |
| About to file an issue, start a task, open or update a PR | **pr-gatekeeper-protocol** |
| PR got `gatekeeper:changes` — parse findings, fix, re-trigger | **pr-gatekeeper-protocol** (§5) |
| PR got `gatekeeper:needs-human` or hit the 3-round limit | **pr-gatekeeper-protocol** (§6) |
| Reacting to `gatekeeper-review-completed` events / chaining agents | **pr-gatekeeper-protocol** (Automation hooks) |

## Ground rules that hold everywhere

- One decision per review round: `MERGE` (bot squash-merges), `CHANGES`
  (fix and push to the same branch), `HUMAN` (stop and ask).
- One task per issue and PR; ≤ 600 changed lines / 20 files unless the repo
  config says otherwise.
- Never edit `.github/` or `AGENTS.md` in a feature PR; never merge your own
  PR; never bypass the gatekeeper; never put secrets in code, commits, or PR
  text.
- After 3 CHANGES rounds the review goes to HUMAN by design.
