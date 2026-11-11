# Reviewable artifacts

An agent publishes an HTML file, you open it in a browser, click any element, and
write a comment. The comment goes to the agent that published it, or to an agent
you name with `@`. Agents read their inbox while they wait, so a comment reaches a
running session in seconds and waits for an idle one until it comes back.

There is no skill file and nothing to register. An agent only needs the commands
on this page.

## Run it

Two builds share one API, one storage layout and one test suite:

```bash
cargo build --release --manifest-path rust/Cargo.toml   # rust/target/release/artifactd
./rust/target/release/artifactd serve                   # 2.2 MB RSS, ~8 ms to first response
python3 src/artifact_server.py                          # the reference build, stdlib only
```

The Rust build is the one to run day to day; the Python build is the readable
reference and the parity oracle. Two cosmetic differences are deliberate: the Rust
server writes compact JSON and a malformed `to` field gets its own message, and an
inbox call carrying an unknown session id echoes that id rather than the handle's
attached one. Point the suite at either:

```bash
python3 -m pytest test/test_artifacts.py -q
ARTIFACT_SERVER_BIN=rust/target/release/artifactd \
ARTIFACT_CLI_BIN=rust/target/release/artifactd python3 -m pytest test/test_artifacts.py -q
ARTIFACT_SERVER_BIN=rust/target/release/artifactd uv run --script scripts/check-artifacts-browser.py
```

The CLI starts the server on demand when the URL is loopback, so this is usually
enough:

```bash
python3 src/artifact_cli.py publish report.html --title "Q4 review"
```

## Handles and sessions

A **handle** names an agent, like `@codex` or `@pi-deepseek`. A **session** is one
running instance of it. Publishing attaches the publishing session to the artifact
as its origin. Other sessions can attach later, from any agent:

```bash
python3 src/artifact_cli.py attach q4-review --as @codex
python3 src/artifact_cli.py sessions q4-review      # who is attached, who is active
python3 src/artifact_cli.py detach q4-review        # this session leaves
```

The CLI derives both values for you. `artifact_cli.py whoami` prints them. Order of
precedence: `--as` and `--session`, then `AGENT_ARTIFACTS_AS`, then the agent's own
environment (`AI_AGENT`, `PI_MODEL`, `PI_SESSION_ID`, `CLAUDECODE`, and similar).

An artifact outlives every session attached to it. Closing a terminal, restarting
an agent, or working from a different agent later makes no difference: the
artifact, its versions and its comments stay, and a new session attaches with one
command.

## The loop

1. **Publish.** The agent writes an HTML file and publishes it. It gets back a
   `slug`, a URL, a version and its session id.
2. **Ask for review.** The agent opens the URL and says what to look at.
3. **Comment.** Two identical ways in: the blue **Comment** pill inside the page,
   or *Comment mode* in the top bar. Then click the element you mean and write the
   comment. Pins are numbered speech bubbles; clicking one opens its thread.
   *Artifact only* hides the review panel without leaving the page, and *Back to
   review* (or Escape) brings it back.
4. **Address it.** Leave it unaddressed and it goes to the agent that published
   the artifact. Type `@` for a list of attached agents with their roles, or name
   one with the *Send to* chips.
5. **Wait.** The agent runs `inbox --wait`, which returns the moment an addressed
   comment lands. Its presence is what makes it "active"; an agent that is not
   waiting is queued for, not pushed to.
6. **Fix and republish.** The agent edits the HTML and publishes again with the
   same `--slug`. That becomes `v2` at the same URL. Old comments keep the version
   they were written on.
7. **Close the thread.** The agent replies and resolves each comment, so the next
   review starts clean.

## Routing, exactly

| Situation | What happens |
|---|---|
| No `@` in the comment | Delivered to the handle that published the artifact |
| `@codex` in the comment, or picked in *Send to* | Delivered to `@codex` only |
| Several names | Delivered to each named agent, each with its own claim |
| Agent is waiting right now | Claimed and returned within a second |
| Agent is not waiting | Stays queued, and is claimed on its next `inbox` |
| Two agents attached at once | Each has its own inbox and its own claims. No double delivery |
| Agent cannot finish it | `release <comment-id>` puts it back in the queue |
| Agent wants everything | `inbox --all` also takes comments addressed to others |

Delivery is pull based on purpose: a comment cannot be injected into a terminal
that is not reading. `inbox --wait` is the read, and the lease it renews is what
the UI shows as *active*. Nothing is lost while an agent is away.

## Commands

| Command | Purpose |
|---|---|
| `publish <file> [--title T] [--slug S] [--note N]` | Publish, or publish a new version of `S` |
| `attach <slug>` / `detach <slug>` | Join or leave an artifact from this session |
| `sessions <slug>` | Attached sessions, roles, and who is active |
| `inbox <slug> [--wait S] [--all]` | Claim addressed comments, long-polling while empty |
| `wait <slug> [--timeout S]` | Same thing, looping until the timeout |
| `comments <slug> [--open] [--since N]` | Read everything, without claiming |
| `reply <comment-id> "<body>"` | Answer a comment |
| `resolve <comment-id>` / `reopen <comment-id>` | Close or reopen a thread |
| `release <comment-id>` | Hand a claimed comment back |
| `map` | Every artifact with its sessions and live state |
| `whoami` | The handle and session this process resolves to |
| `open <slug> [--raw]` | Open the review page, or the bare artifact |
| `rm <slug>` | Delete an artifact and its comments |

Every command takes `--json` before or after the subcommand.

## What a comment contains

```json
{
  "id": "c1a2b3",
  "seq": 4,
  "version": 2,
  "author": "@owner",
  "body": "@codex split the table out of the summary",
  "anchor": {"selector": "body > h2:nth-of-type(1)", "quote": "Summary", "x": 0.5, "y": 0.5},
  "status": "open",
  "to": ["@codex"],
  "claims": {"@codex": {"session": "s-9f8e7d6c", "at": "2026-11-08T16:41:02Z"}},
  "delivery": [{"handle": "@codex", "state": "claimed", "live": true}],
  "created": "2026-11-08T16:40:12Z",
  "replies": [{"id": "r9f8e7", "author": "@codex", "body": "Done in v3.", "created": "..."}]
}
```

`anchor.selector` is a CSS path to the element. `anchor.quote` is the selected
text, or the element's own text. The page re-finds the anchor on load, falls back
to a text search, and shows a grey dashed pin when the element is gone, so a stale
comment is visible rather than silently detached.

## Two agents at once

Each agent runs its own wait against the same artifact:

```bash
# terminal 1
python3 src/artifact_cli.py inbox q4-review --wait 900 --json
# terminal 2
python3 src/artifact_cli.py inbox q4-review --as @codex --wait 900 --json
```

A comment saying `@codex` goes only to terminal 2. One with no name goes to the
publisher. `map` shows which of them is active.

## Rules

- Local laptops only. The service binds to loopback, refuses any other host unless
  `AGENT_ARTIFACTS_ALLOW_REMOTE=1` is set, and has no authentication. Do not expose
  it through a tunnel, a port forward or a shared machine. Pages, comments and
  session records never leave the machine.
- Artifact HTML is untrusted. It is served from a second loopback port, so it has a
  different origin from the review page and the API: it cannot read the reviewer's
  page, change anything in the API (every state change needs the
  `x-artifacts-client` header and claiming is a POST), or run a remote script. Keep
  that split when you change ports.
- Publishing copies the file into the state directory. Do not publish anything
  with a secret in it; the copy outlives the source file.
- One artifact per thing under review. Use `--slug` for new versions, and a new
  artifact when the subject changes.
- Comments belong to a version. After republishing, say which version answered
  which comment in the reply.

## State and configuration

| Setting | Default |
|---|---|
| State directory | `~/.local/state/agent-relay/artifacts/` (`AGENT_ARTIFACTS_DIR`) |
| Server URL | `http://127.0.0.1:43126` (`AGENT_ARTIFACTS_URL` for clients) |
| Port / host | `AGENT_ARTIFACTS_PORT`, `AGENT_ARTIFACTS_HOST` |
| Artifact origin | `AGENT_ARTIFACTS_CONTENT_PORT`, default the next port up |
| Presence lease | 30 s, renewed while waiting (`AGENT_ARTIFACTS_LEASE_SECONDS`) |
| Artifact size | 8 MB (`AGENT_ARTIFACTS_MAX_BYTES`) |
| Comment length | 20,000 characters (`AGENT_ARTIFACTS_MAX_COMMENT_CHARS`) |
| Longest wait | 600 s (`AGENT_ARTIFACTS_MAX_WAIT_SECONDS`) |

Layout: `<state>/<slug>/versions/<n>.html`, plus `artifact.json` holding versions,
sessions, presence and comments.

## Telling an agent about it

Paste this into any `AGENTS.md`, `CLAUDE.md`, or system prompt. Keep it out of
skill directories if you do not want it auto-loaded.

```text
Reviewable artifacts: to have the person review something visual, write an HTML
file and run
  python3 <repo>/src/artifact_cli.py publish <file> --title "<title>" --json
Give them the returned url, then run
  python3 <repo>/src/artifact_cli.py inbox <slug> --wait 900 --json
and act on each comment addressed to you. Publish fixes with the same --slug,
answer each comment with `reply`, close it with `resolve`, and use
`attach <slug>` to rejoin an artifact from a later session.
```

## HTTP API

The CLI is a thin client; agents with no shell can use HTTP directly. Every
state-changing call needs `x-artifacts-client: 1`. A cross-origin `<img>`, `<form>`
or `fetch` inside an artifact cannot set that header without a preflight the server
never grants, and claiming is a `POST`, so viewing a hostile page cannot consume a
comment or attach a session.

| Method and path | Purpose |
|---|---|
| `POST /api/artifacts` | `{html, title?, slug?, note?, as?, session?, cwd?}` |
| `GET /api/artifacts` | List, with handles and sessions |
| `GET /api/artifacts/<slug>` | One artifact, its versions, sessions and comments |
| `DELETE /api/artifacts/<slug>` | Delete |
| `GET`/`POST /api/artifacts/<slug>/sessions` | List, or attach `{as, session?, cwd?}` |
| `DELETE /api/artifacts/<slug>/sessions/<id>` | Detach one session |
| `POST /api/artifacts/<slug>/inbox` | `{as, session?, wait?, all?}` claims addressed comments |
| `GET /api/artifacts/<slug>/comments?since=&wait=&open=` | Read only, never claims |
| `POST /api/artifacts/<slug>/comments` | `{body, author?, version?, anchor?, to?}` |
| `POST /api/comments/<id>/reply` | `{body, author?}` |
| `POST /api/comments/<id>/status` | `{status: "open" \| "resolved"}` |
| `POST /api/comments/<id>/release` | `{author}` hands a claim back |
| `GET /a/<slug>` | Review page |
| `GET /a/<slug>/raw?v=&review=1` | The artifact, on the content origin, with the overlay |
| `GET /healthz` | Liveness |
