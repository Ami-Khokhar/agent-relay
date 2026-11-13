# The safe updater

This page documents the `src/safe_update.py` CLI: how a published release is
inspected, approved on one device, staged in isolation, and — only after a
prepared, durably journaled transaction — activated through the restricted
per-user service boundary, with automatic rollback and explicit recovery.

**What is verified and what is not.** The flow is verified end to end by
`python3 -m unittest discover -s test -p 'test_safe_update_acceptance.py'`,
which drives the real public CLI commands, the real relay HTTP server
(`src/server.py`) and the real workflow engine inside temporary HOME, state and
install trees. The only fakes are the canonical GitHub HTTPS transport
(release metadata answered in-process), the git process boundary (a forwarding
script translates the canonical clone URL to a local fixture origin), the
interactive TTY, and the per-user service manager (the real `inspect_service`
validates the unit; the restart is simulated by stopping and re-spawning the
real relay process on the same port). That is implementation verification on
isolated synthetic boundaries: **no release is published, no live install is
performed, and no host service is changed or authorized by it.** Running these
tests is not deployment evidence; an owner deploys by running the commands
below on their own device, and the project currently publishes no releases or
tags, so every no-release path is the current live reality.

## The command sequence

```console
$ python3 src/safe_update.py inspect 1.2.3          # metadata only, writes nothing
$ python3 src/safe_update.py approve 1.2.3          # interactive, 15-minute device-local consent
$ python3 src/safe_update.py stage 1.2.3 --install-dir ~/.local/share/agent-relay
$ python3 src/safe_update.py drain 1.2.3 --install-dir ~/.local/share/agent-relay \
      --base-url http://127.0.0.1:43124             # rehearsal: hold and release, installs nothing
$ python3 src/safe_update.py prepare-activation 1.2.3 --install-dir ~/.local/share/agent-relay \
      --base-url http://127.0.0.1:43124             # holds the drain, writes the journal
$ python3 src/safe_update.py activate <transaction-id> --install-dir ~/.local/share/agent-relay \
      --base-url http://127.0.0.1:43124
$ python3 src/safe_update.py status
$ python3 src/safe_update.py recover-abort <transaction-id> --base-url http://127.0.0.1:43124
$ python3 src/safe_update.py recover <transaction-id> --base-url http://127.0.0.1:43124
```

`consume 1.2.3` validates and burns a consent record on demand; `stage` consumes
it as its first durable effect. Every command prints what it did and what it
did not do; nothing installs, activates or restarts before `activate`.

## Release identity, manifest and maintainer trust

`inspect` resolves one exact stable release from the canonical GitHub HTTPS
API: the tagged release, the immutable 40-hex commit peeled from `refs/tags/`
(`target_commitish` is never proof), the `agent-relay-manifest.json` asset and
its sha256 digest, and the release/asset timestamps. The manifest
(`agent-relay.update/v1`) binds the repository, version, tag, commit and a
path/mode/SHA256 inventory of every tracked file. Metadata endpoints use the
GitHub JSON Accept header and follow no redirect; the manifest asset uses
`Accept: application/octet-stream` and may follow exactly one redirect, only to
a canonical GitHub download host. Anything noncanonical — another repository,
a draft or prerelease, a redirect elsewhere, a missing or mutated manifest, no
matching release — is refused with a named reason and changes nothing.

The manifest digest authenticates content only because the project's canonical
GitHub maintainers publish it over HTTPS. **No independent signature scheme is
claimed or checked.** The `manifestB64` copy inside a stage receipt is
re-verified against the identity digest, never trusted on its own.

## Supported checkout and service layout

`stage`, `prepare-activation`, `activate` and `recover` accept one install
layout only:

- the `--install-dir` is an ordinary, user-owned, clean git checkout whose
  `origin` is exactly `https://github.com/Ami-Khokhar/agent-relay` (or
  `...git`), not a symlink, not group- or world-writable, not under a system
  prefix or an installed package location, not a linked worktree, and tracking
  no symlinks or submodules;
- the per-user service definition is a macOS launchd job
  `~/Library/LaunchAgents/com.agent-relay.plist` or a systemd `--user` unit
  `~/.config/systemd/user/agent-relay.service` (an absolute `XDG_CONFIG_HOME`
  location is also honored), plain, user-owned, non-symlinked, whose
  `ExecStart`/program arguments are exactly this install's `src/server.py`
  under its Python interpreter, with the working directory at the install dir
  and no configuration, wrapper, shell, sudo or hook inside or around it;
- the relay's state lives in `AGENT_RELAY_STATE_DIR` (the same private state
  directory the updater writes its consent, stages and transaction journal
  into), and the drain and barrier endpoints are reached over loopback with
  the relay's API token (`AGENT_RELAY_TOKEN`).

Everything else is refused with a named reason: package-managed or system
installs, dirty or foreign checkouts, ambiguous or weakened service
definitions, non-loopback relays, missing tokens.

## Consent, staging and the activation transaction

`approve` requires a real interactive terminal and the exact typed version and
full commit, then records a private 0600 device-local consent bound to the
installation realpath, a locally generated device id, the full identity digest
and a fresh nonce. **The consent is exactly local and expires after 15
minutes**: a later stage must still find it valid — an expired, future,
replayed, mutated, wrong-device or changed-path record is refused with a
remedy (`approve the release again`), and a refused consumption leaves the
record exactly as it was.

`stage` consumes the consent once, clones the approved commit into a brand-new
private repository under `<state dir>/staging/` (hooks and checkout filters
disabled, verified against the manifest inventory, detached at the exact
commit), writes a 0600 stage receipt binding the identity, manifest bytes,
source root and commit, and never writes to the active checkout.

`prepare-activation` re-proves the published identity, the manifest bytes, the
verifying stage and the consumed consent one last time, requires the active
checkout to be unchanged since staging (still clean, canonical, at the exact
commit the stage recorded), then acquires the drain and writes
`<state dir>/update/transaction.json` plus `admission-barrier.json`: the old
exact relay commit, the target identity, the stage, hashed references to the
external state, and the drain capability. The acquired drain's revision must be
that same recorded commit; a checkout already fast-forwarded past the running
relay (the state `scripts/update.sh` leaves while its restart is deferred) is
refused by name, with the hold released and nothing journaled, because
`activate` can only return to the revision the drain started from. The drain
stays held after the command returns; while it is held the relay refuses every
new external admission (tasks, workflow submissions, resumes) and already
accepted work finishes normally.

`activate` walks the journal through `draining` → `quiescent` → `activating` →
`restarted`, swaps the checkout with trusted, hook- and filter-disabled git by
fetching the staged object locally, restarts only the positively validated
per-user service, and accepts the result only when the restarted relay reports
the exact full target commit through an authenticated endpoint and still names
this transaction's admission barrier. Only then is the record finalized to
`activated`, the barrier removed, and admission reopened on the live relay.

## Refusals and recovery

- **A failed activation** (any step after the drain is held) writes a durable
  `recovery_required` end and then rolls back automatically to the exact prior
  revision the journal names — the checkout is restored only while it still
  inspects clean (an intervening user edit is refused, never overwritten), the
  service is restarted only when the running relay does not already report the
  restored revision, and the barrier holds until the authenticated endpoint
  reports that exact prior revision. `status` prints the record; `recover`
  finishes or retries it, idempotently, without repeating a service action.
- **A prepared transaction you change your mind about** is ended by
  `recover-abort`, which re-proves the unchanged external state, the exact
  starting revision, the owned drain and quiescence before releasing and
  expunging; any failed proof leaves admission closed for manual recovery.
- **A crash mid-activation** leaves the journal and barrier on disk: a
  restarted relay boots inside the barrier and refuses admission naming the
  transaction, and `recover` finishes the rollback to the journaled prior
  revision.
- **A relay that will not reopen admission** after a durable terminal record
  is named by the error with the exact remedy (`fix the relay and run
  safe_update recover <transaction-id>`); nothing is ever reopened on a guess.

## Relation to the legacy updater and to automatic checks

`scripts/update.sh` is the legacy manual flow: it fast-forwards the source
checkout to `main` and restarts the service. It has no release identity, no
consent, no staging and no rollback, and it is **not** part of the safe-update
flow: the safe updater never calls it, and staging never executes it. The
metadata-only release check (`src/release_discovery.py`, the 6-hour timer, and
MCP startup notice) never installs anything either — it offers bounded release
metadata to the operator, who runs the safe-update commands above by hand.
