#!/usr/bin/env python3
"""Safe update CLI: inspect a published release and record device-local consent.

This is the first consumer of ``release_identity``. ``inspect`` resolves and
validates a stable release identity (metadata only), ``approve`` adds an
interactive, private, 15-minute device-local consent record, and ``stage``
builds an isolated private repository holding the exact approved commit —
only for a positively identified clean canonical source checkout, only
while the recorded consent is still valid, and never by executing any
staged content. Nothing is installed, activated or restarted by these
commands. This issue adds ``activate``, the first activation consumer: it
walks a prepared transaction through its journaled phases, swaps the verified
source with trusted hook- and filter-disabled git, restarts only a positively
validated per-user launchd/systemd service, and verifies the restarted relay
through an authenticated endpoint carrying the exact full commit plus the
transaction's own barrier ownership. A failed or interrupted activation is
recovered to the exact prior revision, automatically at first and explicitly
with ``recover`` while an interruption stands: the rollback reads the exact
prior commit only from the durable journal, never overwrites intervening user
edits, and holds the admission barrier until the authenticated endpoint again
reports the exact prior revision.
"""

import argparse
import http.client
import os
import sys
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # non-POSIX: importing this module must keep working there
    fcntl = None
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

import hashlib

import release_identity
import update_drain
import update_service
import update_staging
import update_transaction
from release_identity import UpdateError

STATE_LABEL = "release update consent"
BARRIER_PATH = "/v1/admin/update/state"
AGENTS_PATH = "/v1/agents"
JSON_ACCEPT = "application/vnd.github+json"
# The manifest asset is file content: GitHub answers the JSON Accept header with
# asset metadata, so it must be requested with the octet-stream Accept header.
ASSET_ACCEPT = "application/octet-stream"
# GitHub serves release assets by 302ing to one of these signed download hosts;
# any other redirect target is an uncanonical identity and refused.
ASSET_REDIRECT_HOSTS = frozenset(("github.com", "objects.githubusercontent.com",
                                  "release-assets.githubusercontent.com"))


class _Redirected(Exception):
    """Carries an un-followed redirect target out of the opener for inspection."""

    def __init__(self, location):
        super().__init__(location)
        self.location = location


class _CaptureRedirect(HTTPRedirectHandler):
    """Raise with the redirect target instead of following it automatically."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib's 30x handlers already make newurl absolute for this request.
        raise _Redirected(newurl)


def _fetch(url):
    """One bounded HTTPS request against the canonical GitHub API host.

    Release and tag metadata must stay on the canonical API host with no
    redirect at all. The manifest asset is requested with the octet-stream
    Accept header, and GitHub answers it with one redirect to a signed download
    host: exactly one redirect there is followed, and only to a canonical
    GitHub download host; every other redirect is refused.
    """
    if not url.startswith(release_identity.API_BASE + "/"):
        raise UpdateError("refusing a non-canonical release identity request")
    asset = url.startswith(release_identity.ASSET_URL)
    accept = ASSET_ACCEPT if asset else JSON_ACCEPT
    current, redirects_left = url, 1 if asset else 0
    while True:
        request = release_identity.Request(
            current, headers={"Accept": accept,
                              "User-Agent": "agent-relay-release-update"})
        handler = _CaptureRedirect() if redirects_left else release_identity._NoRedirect()
        try:
            with build_opener(handler).open(request, timeout=5) as response:
                if response.geturl() != current:
                    raise UpdateError("release service returned a non-canonical URL")
                data = response.read(release_identity.release_discovery.MAX_RESPONSE + 1)
        except _Redirected as redirected:
            if not redirects_left:
                raise UpdateError("release service redirected; refusing non-canonical identity") from None
            redirects_left -= 1
            target = urlsplit(redirected.location)
            if target.scheme != "https" or (target.hostname or "") not in ASSET_REDIRECT_HOSTS:
                raise UpdateError("release asset redirected to a non-canonical host; "
                                  "refusing uncanonical identity") from None
            current = redirected.location
            continue
        except HTTPError as exc:
            if exc.code == 404:
                raise UpdateError("no published stable release matches this tag; check the tag name") from exc
            if exc.code in (301, 302, 303, 307, 308):
                raise UpdateError("release service redirected; refusing non-canonical identity") from exc
            raise UpdateError("release service unavailable (HTTP %d); try again later" % exc.code) from exc
        except (http.client.HTTPException, TimeoutError) as exc:
            raise UpdateError("release service timed out or hung up; try again later") from exc
        except (URLError, OSError) as exc:
            raise UpdateError("release service unavailable; check connectivity and try again") from exc
        return data


def _resolve(tag):
    identity, manifest, manifest_bytes = release_identity.resolve_release(tag, fetch_json=_fetch)
    return identity, manifest, manifest_bytes


def _state_dir():
    state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state_home) / "agent-relay"


def cmd_inspect(tag):
    identity, manifest, _bytes = _resolve(tag)
    print("Release identity validated (no release will be installed):")
    print(release_identity.identity_summary(identity))
    print("Manifest schema: %s" % manifest["schema"])
    return 0


def cmd_approve(tag):
    identity, _manifest, _bytes = _resolve(tag)
    consent = release_identity.approve(identity, state_dir=_state_dir(), now=time.time())
    print("Consent recorded for %s until %s (nonce %s)." %
          (consent["tag"], consent["expiresAt"], consent["nonce"][:8]))
    print("Nothing was installed; activation must consume this consent later.")
    return 0


def cmd_stage(tag, install_dir):
    identity, manifest, manifest_bytes = release_identity.resolve_release(tag, fetch_json=_fetch)
    receipt = update_staging.stage_release(
        identity, root=install_dir, state_dir=_state_dir(), git=update_staging.default_git(),
        manifest=manifest, manifest_bytes=manifest_bytes, now=time.time())
    print("Staged release %s (commit %s) in the isolated repository at\n  %s\n"
          "The stage was verified against the release manifest; nothing was installed\n"
          "or executed, and the active checkout was not changed." %
          (identity["tag"], identity["commit"], receipt["stage"]))
    return 0


def _stage_receipt(tag, install_dir, state_dir):
    """The verified stage receipt this tag, staged from ``install_dir".

    The drain never names a release without proof this device staged it: the
    receipt binds the exact resolved identity, the manifest bytes and the
    root it was staged from, and the staged tree is re-verified against its
    own bound manifest before any drain begins.
    """
    staging = Path(state_dir) / "staging"
    if not staging.is_dir():
        raise UpdateError("no staged release for %s; stage it first" % tag)
    matches = []
    for child in sorted(staging.iterdir()):
        receipt_path = child / update_staging.STAGE_RECEIPT_NAME
        try:
            release_identity.private_file_checks(receipt_path, "stage receipt")
            receipt = release_identity.strict_json(receipt_path.read_bytes(),
                                                   "stage receipt")
        except (UpdateError, OSError):
            continue
        if receipt.get("schema") != update_staging.STAGE_SCHEMA:
            continue
        stored = receipt.get("identity")
        if not isinstance(stored, dict) or stored.get("tag") != tag:
            continue
        if release_identity.identity_digest(stored) != receipt.get("identityDigest"):
            continue
        if receipt.get("sourceRoot") != str(Path(install_dir).resolve()):
            continue
        matches.append((stored, receipt))
    if not matches:
        raise UpdateError("no verified stage of %s staged from %s"
                          % (tag, Path(install_dir).resolve()))
    # Newest succeeded stage wins; stages removed on failure never appear.
    stored, receipt = matches[-1]
    try:
        update_staging.verify_stage(receipt["stage"], stored,
                                    git=update_staging.default_git())
    except (update_staging.StagingError, KeyError) as exc:
        raise UpdateError("the stage of %s no longer verifies: %s" % (tag, exc))
    return receipt, stored


def _consumed_consent(identity, state_dir):
    """The locally recorded consent proves this exact release was approved 
    and consumed by staging; the record is never burned again here."""
    consent, _path = release_identity._load_consent(Path(state_dir))
    stored = consent.get("identity")
    if not isinstance(stored, dict) \
            or release_identity.identity_digest(stored) != consent.get("identityDigest") \
            or consent.get("identityDigest") != release_identity.identity_digest(identity):
        raise UpdateError("the recorded consent does not match this release identity")
    if consent.get("tag") != identity["tag"] or consent.get("commit") != identity["commit"]:
        raise UpdateError("the recorded consent was issued for a different release")
    if consent.get("consumed") is not True:
        raise UpdateError("the recorded consent was never consumed by staging a release")
    return consent


def cmd_drain(tag, install_dir, *, base_url=None, transaction_id=None,
              quiesce_wait_s=None):
    """The rehearsal: verify the proof, hold the drain, wait boundedly, release.

    Nothing is installed or restarted and no service control is called; the
    relay is only asked for its drain and releases it again. This is the
    explicit updater consumer, so the server acknowledges quiescence instead
    of the updater inferring it from healthz.
    """
    state_dir = _state_dir()
    _receipt, identity = _stage_receipt(tag, install_dir, state_dir)
    _consumed_consent(identity, state_dir)
    base_url, token = _authorize_drain(base_url)
    transaction_id = transaction_id or update_drain.new_transaction_id()
    try:
        acquired = update_drain.acquire_drain(base_url, transaction_id, token=token)
    except update_drain.DrainError as exc:
        raise UpdateError("the drain could not be acquired: %s" % exc)
    revision, capability = acquired["revision"], acquired["capability"]
    status = None
    try:
        try:
            status = update_drain.wait_for_quiescence(
                base_url, transaction_id, token=token, revision=revision,
                capability=capability,
                wait_s=update_drain.DEFAULT_QUIESCE_WAIT_S
                if quiesce_wait_s is None else quiesce_wait_s)
        except update_drain.DrainError as exc:
            status = exc.last_status
            print("The bounded wait ended without quiescence: %s" % exc)
        print("Drain %s held at commit %s." % (transaction_id, revision))
        if status is not None:
            print("Quiescence acknowledged by the server: %s" %
                  ("yes" if status["quiesced"] else
                   "active=%(active)s queued=%(queued)s workflows=%(workflows)s"
                   % status))
    finally:
        try:
            released = update_drain.release_drain(
                base_url, transaction_id, token=token, revision=revision,
                capability=capability)
            print("Drain released: transaction %s (only the explicit release "
                  "reopens admission)." % released["transactionId"])
        except update_drain.DrainError as exc:
            print("warning: the drain could not be released: %s; the hold stands "
                  "until an explicit release recovers it." % exc, file=sys.stderr)
    return 0 if status and status["quiesced"] else 1


def _authorize_drain(base_url):
    base_url = base_url or update_drain.relay_base_url()
    if not base_url:
        raise UpdateError("no relay base URL; pass --base-url or set AGENT_RELAY_URL")
    token = update_drain.relay_token()
    if not token:
        raise UpdateError("no relay token; set AGENT_RELAY_TOKEN to the relay's API token")
    return base_url, token


def cmd_prepare_activation(tag, install_dir, *, base_url=None, transaction_id=None):
    """Validate everything again and prepare a recoverable activation transaction.

    Nothing is installed, activated or restarted here: the exact release,
    manifest, stage and a clean, canonical checkout are revalidated; the
    device-local consent's durable single-use record is re-proven one last
    time; and only when all of that holds is the exact-revision authenticated
    drain acquired and a durable, recoverable transaction written. The drain
    is held after this command returns.
    """
    state_dir = _state_dir()
    resolved = Path(install_dir).resolve()
    identity, manifest, manifest_bytes = _resolve(tag)
    receipt, stored = _stage_receipt(tag, install_dir, state_dir)
    if release_identity.identity_digest(stored) != release_identity.identity_digest(identity):
        raise UpdateError("the release or manifest for %s changed since approval/staging; "
                          "refusing a mixed activation" % tag)
    if release_identity.identity_digest(stored) != receipt["identityDigest"]:
        raise UpdateError("the stage receipt was written for a different identity than it stores")
    checkout = update_staging.inspect_checkout(resolved, git=update_staging.default_git())
    if checkout["commit"] != identity["commit"] or checkout["root"] != receipt["sourceRoot"]:
        raise UpdateError("the checkout at %s changed since staging; refusing a mixed "
                          "activation" % resolved)
    _consumed_consent(identity, state_dir)   # consent is consumed once, durably, by staging
    release_identity.validate_manifest(manifest, identity)
    if hashlib.sha256(manifest_bytes).hexdigest() != identity["manifestDigest"]:
        raise UpdateError("the resolved manifest bytes do not match the release identity")
    base_url, token = _authorize_drain(base_url)
    transaction_id = transaction_id or update_drain.new_transaction_id()
    try:
        acquired = update_drain.acquire_drain(base_url, transaction_id, token=token)
    except update_drain.DrainError as exc:
        raise UpdateError("the drain could not be acquired: %s" % exc)
    try:
        record = update_transaction.begin_transaction(
            {**receipt, "transactionId": transaction_id, "drain": acquired},
            state_dir=state_dir)
    except Exception as exc:  # the hold acquired above must never outlive a failed prepare
        note = _release_drain_after_failure(base_url, transaction_id, token, acquired)
        raise UpdateError("the transaction could not be prepared: %s; %s" % (exc, note)) from exc
    print("Prepared activation transaction %s:" % transaction_id)
    print("  target  %s (%s)" % (identity["tag"], identity["commit"]))
    print("  old     exact relay revision %s" % acquired["revision"])
    print("  stage   %s" % receipt["stage"])
    print("The exact-revision drain is held and admission is closed; nothing was activated"
          " or restarted. Use status and recover-abort to inspect or abort it.")
    return 0


def _release_drain_after_failure(base_url, transaction_id, token, acquired):
    """The hold the effect boundary acquired is released again, and said so."""
    revision, capability = acquired.get("revision"), acquired.get("capability")
    if not revision or not capability:
        return "the drain was never acquired"
    try:
        update_drain.release_drain(base_url, transaction_id, token=token,
                                   revision=revision, capability=capability)
    except update_drain.DrainError as exc:
        return ("the acquired drain hold could NOT be released: %s; the hold stands "
                "until an explicit release recovers it" % exc)
    return "the acquired drain hold was released again"


def _authenticated_get(base_url, token, path):
    """One authenticated GET against the relay; a refusal is an error, never a guess."""
    status, body = update_drain._request("GET", base_url, path, token=token)
    if status != 200:
        message = (body or {}).get("message", status)
        raise update_drain.DrainError("state_refused_%d" % status,
                                      "the relay refused %s: %s" % (path, message),
                                      status=status)
    if not isinstance(body, dict):
        raise update_drain.DrainError("unknown_health", "the relay gave no JSON object")
    return body


def cmd_activate(transaction_id, install_dir, *, base_url=None, inspect=None,
                 restart=None, deadline_s=None):
    """Activate the exact staged revision through the restricted service boundary.

    Everything is proven again before one durable effect: the hashed external
    references are unchanged, the relay still runs the exact starting revision
    and this transaction's drain is held and quiesced, the stage still
    verifies, the checkout is the prepared original, and the per-user service
    definition positively names this install dir. Only then, inside the
    transaction/install lock, does the flow walk the journal through its
    phases, swapping the source with the journaled exact-revision activation,
    restarting the service and verifying the restarted relay via an
    authenticated endpoint carrying the exact full commit plus the barrier's
    transaction ownership — never a mere ok:true or an open port.

    Any failed or uncertain step leaves the transaction durably recorded
    (``recovery_required``), the admission barrier retained and external
    state preserved, and is then recovered automatically to the exact prior
    revision the journal names: the rollback restores the source, restarts
    the same inspected service boundary only when needed, and holds the
    barrier until the authenticated endpoint again reports the exact prior
    revision. A rollback step that itself fails keeps the recovery_required
    record, its barrier and an actionable remedy: the operator finishes it
    with ``recover``. A step that failed before the first effect still leaves
    the prepared transaction recoverable through recover-abort.
    """
    state_dir = _state_dir()
    resolved = Path(install_dir).resolve()
    record = update_transaction.recover_transaction(state_dir=state_dir)
    if record is None:
        raise UpdateError("no update transaction is recorded; nothing to activate")
    if record["transactionId"] != transaction_id:
        raise UpdateError("the recorded transaction is %s, not %s; refusing to replay "
                          "another transaction's activation"
                          % (record["transactionId"], transaction_id))
    if record["phase"] != "prepared":
        raise UpdateError("the transaction is in phase %r: manual recovery required; "
                          "only a prepared transaction may be activated"
                          % record["phase"])
    prepared_root = (record.get("inventory") or {}).get("sourceRoot")
    if str(resolved) != prepared_root:
        raise UpdateError("this transaction was prepared for the checkout %s, not %s; "
                          "refusing to activate a different install dir than the one "
                          "the stage and drain were proven against"
                          % (prepared_root, resolved))
    drain = record.get("drain")
    if not isinstance(drain, dict) or drain.get("revision") != record["revision"] \
            or not isinstance(drain.get("capability"), str) or not drain.get("capability"):
        raise UpdateError("the transaction does not name the drain it owned; manual "
                          "recovery required")
    identity = (record.get("target") or {}).get("identity")
    if not isinstance(identity, dict) or \
            release_identity.identity_digest(identity) != (record.get("target") or {}).get("identityDigest"):
        raise UpdateError("the transaction does not bind an intact release identity; "
                          "manual recovery required")
    base_url, token = _authorize_drain(base_url)
    deadline_s = update_service.AWAIT_REVISION_S if deadline_s is None else deadline_s
    inspect = inspect if inspect is not None else update_service.inspect_service
    restart = restart if restart is not None else update_service.restart_service
    git = update_staging.Git(update_staging.default_git())
    # everything below is proven before any effect happens
    _proof_references(record, state_dir)
    _proof_running_revision(record, base_url)
    try:
        status = update_drain.drain_status(base_url, transaction_id, token=token,
                                           revision=record["revision"],
                                           capability=drain["capability"])
    except (update_drain.DrainError, KeyError) as exc:
        raise UpdateError("this transaction's exact-revision drain must be held and "
                          "acknowledged quiescent; manual recovery required: %s" % exc) from exc
    if not status.get("quiesced"):
        raise UpdateError("the relay had not quiesced when activation was ordered "
                          "(active=%(active)s queued=%(queued)s workflows=%(workflows)s); "
                          "manual recovery required" % status)
    try:
        update_staging.verify_stage(record["stage"]["path"], identity, git=git)
    except (update_staging.StagingError, KeyError, OSError) as exc:
        raise UpdateError("the staged release no longer verifies (%s); leaving the "
                          "transaction recoverable" % exc)
    # recheck the active HEAD, cleanliness and canonical identity before any effect
    checkout = update_staging.inspect_checkout(resolved, git=git)
    if checkout["commit"] != record["revision"] or checkout["root"] != str(resolved):
        raise UpdateError("the checkout at %s is %s at HEAD %s, not the transaction's "
                          "cleanly prepared original revision %s; refusing an unsafe "
                          "activation" % (resolved, checkout["root"], checkout["commit"],
                                          record["revision"]))
    # the restricted per-user service boundary, positively validated first
    service = inspect(resolved)
    if fcntl is None:
        raise UpdateError("activation needs POSIX file locking for the transaction "
                          "journal; refusing to activate state where os.flock is "
                          "unavailable")
    lock = update_transaction._flock(update_transaction._lock_path(state_dir))
    failure = None
    reverted = {}   # the swap result the activation had reached, if any
    try:
        journal = update_transaction.read_journal(state_dir=state_dir)
        if journal is None or journal.get("transactionId") != transaction_id \
                or journal.get("phase") != "prepared":
            # a refusal before any effect: no recovery recording, the raised
            # error is the whole story
            raise UpdateError("no exactly prepared transaction is recorded for %s; manual "
                              "recovery required" % transaction_id)
        try:
            update_transaction.transition_locked(transaction_id, "prepared", "draining",
                                                 state_dir=state_dir, quiesced=True)
            update_transaction.transition_locked(transaction_id, "draining", "quiescent",
                                                 state_dir=state_dir, service=service)
            update_transaction.transition_locked(
                transaction_id, "quiescent", "activating", state_dir=state_dir,
                oldRevision=record["revision"], targetCommit=identity["commit"],
                servicePath=service["path"], serviceDigest=service.get("digest", ""))
            reverted = update_staging.activate_transaction_source(record, git=git)
            update_transaction.transition_locked(transaction_id, "activating", "restarted",
                                                 state_dir=state_dir, commit=reverted["target"])
            restart(service)
            request = _activated_revision_request(base_url, token)
            body = update_service.await_revision(reverted["target"],
                                                 authenticated_request=request,
                                                 deadline_s=deadline_s)
            if body.get("revision", {}).get("commit") != reverted["target"]:
                raise UpdateError("the restarted relay did not report the activated "
                                  "commit; refusing")
            barrier = _authenticated_get(base_url, token, BARRIER_PATH)
            closed = (barrier.get("barrier") or {}).get("transactionId")
            if not barrier.get("admissionClosed") or closed != transaction_id:
                raise UpdateError("the restarted relay does not name this transaction's "
                                  "admission barrier; refusing an unowned activation")
            update_transaction.transition_locked(transaction_id, "restarted", "activated",
                                                 state_dir=state_dir)
            refresh = _refresh_barrier(base_url, token)
            if refresh.get("admissionClosed"):
                raise UpdateError("the activated relay still holds admission closed; "
                                  "open it explicitly before serving work")
        except Exception as exc:
            # durably record the failed activation while this lock is still held,
            # then release it and attempt the automatic rollback below
            _record_recovery(transaction_id, state_dir, exc)
            failure = exc
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
    if failure is not None:
        record = update_transaction.read_journal(state_dir=state_dir)
        if record is not None and record.get("transactionId") == transaction_id \
                and record["phase"] != "recovery_required":
            # the activation was already finalized durably (typically a
            # post-activated _refresh_barrier failure): its state is the truth;
            # rewinding a verified activation would revert a good release
            raise UpdateError(
                "activation is durable as %s for %s (%s), but the relay has not "
                "reopened admission; run `safe_update recover %s` to expunge the "
                "record and retry the refresh"
                % (record["phase"], transaction_id, failure, transaction_id)) \
                from failure
        try:
            rollback_transaction(transaction_id, state_dir=state_dir, service=service,
                                 base_url=base_url, token=token, git=git,
                                 restart=restart, deadline_s=deadline_s)
        except Exception as rollback_exc:
            raise UpdateError(
                "activation failed (%s) and the automatic rollback did not complete (%s); "
                "the admission barrier still holds admission closed; manual recovery "
                "required: fix the failing step and run `safe_update recover %s`"
                % (failure, rollback_exc, transaction_id)) from rollback_exc
        update_transaction.clear_transaction(transaction_id, state_dir=state_dir)
        print("Activation failed (%s); the exact prior revision %s was restored and "
              "verified, transaction %s is finished and admission is open. The target "
              "release %s was NOT activated." %
              (failure, reverted.get("previousObject", record["revision"]), transaction_id,
               identity.get("tag")))
        return 1
    update_transaction.clear_transaction(transaction_id, state_dir=state_dir)
    print("Activated commit %s and verified it through the authenticated "
          "endpoint; transaction %s is finished and admission is open." %
          (reverted["target"], transaction_id))
    return 0


def _record_recovery(transaction_id, state_dir, exc, **evidence):
    """Record a durable recovery_required end for a half-built activation.

    The caller holds the transaction lock. A journal of this install for this
    transaction in a live phase is advanced to ``recovery_required``; a record
    already in ``recovery_required`` only gains a note. A terminal record stays
    exactly as it was (its own finalize already carried the proof) and the
    original failure travels to the caller. A journal write that itself fails
    masks the failure with the remedied error, since the barrier still holds.
    """
    try:
        record = update_transaction.read_journal(state_dir=state_dir)
        if record is None or record.get("transactionId") != transaction_id:
            return  # nothing of ours is recorded; report the failure as it is
        if record["phase"] == "recovery_required":
            update_transaction.journal_recovery_note(transaction_id, state_dir=state_dir,
                                                     reason=str(exc), **evidence)
        elif record["phase"] in update_transaction.PHASES \
                and record["phase"] not in update_transaction.TERMINAL_PHASES:
            update_transaction.transition_locked(transaction_id, record["phase"],
                                                 "recovery_required", state_dir=state_dir,
                                                 reason=str(exc), **evidence)
        # a terminal record (activated or rolled_back) stays exactly as it was:
    except Exception as recovery_exc:
        raise UpdateError("activation failed (%s) and the recovery record could not be "
                          "written (%s); the barrier still holds admission closed; "
                          "manual recovery required" % (exc, recovery_exc)) from exc


def rollback_transaction(transaction_id, *, state_dir, service, base_url=None,
                         token=None, git=None, restart=None, deadline_s=None):
    """Roll a failed activation back to the exact, journaled prior revision.

    ``transaction_id`` names this install's recorded ``recovery_required``
    transaction; ``service`` is the positively inspected per-user service
    boundary of the transaction's checkout. This function takes the
    transaction/install lock itself and reads the exact prior commit only from
    the durable journal — never from a branch, main or a release tag. In
    order, and journaling every step before the next effect: the hashed
    external references are re-proven, the drain is proven held and quiescent
    (a restarted relay owing no hold is accepted there, as in recover-abort),
    the prior commit object is proven retained and the working tree restored
    only when ``inspect_checkout`` still accepts it (an intervening user edit
    or unsafe path mutation is refused, never overwritten), the service is
    restarted only when the running relay does not already report the restored
    revision, and the admission barrier is held until an authenticated endpoint
    reports that exact prior revision. Only then is the journal finalized to
    ``rolled_back`` — which removes the barrier — and the drain released with
    its own capability. Every failed proof keeps the ``recovery_required``
    state and its barrier, reports an actionable remedy, and never declares
    the rollback complete or opens admission.

    Returns ``{"previous", "previousObject", "restored", "serviceRestarted"}
    when the relay reopened admission; the record is expunged by the caller.
    Each caller holds no lock across this function."""
    state_dir = Path(state_dir)
    if not isinstance(service, dict) or service.get("kind") not in ("launchd", "systemd"):
        raise UpdateError("rollback needs the positively inspected service boundary of the "
                          "transaction's checkout")
    git = git if isinstance(git, update_staging.Git) \
        else update_staging.Git(git or update_staging.default_git())
    restart = restart if restart is not None else update_service.restart_service
    base_url = base_url or update_drain.relay_base_url()
    if not base_url:
        raise UpdateError("no relay base URL; pass --base-url or set AGENT_RELAY_URL")
    if token is None:
        token = update_drain.relay_token()
    deadline = update_service.AWAIT_REVISION_S if deadline_s is None else deadline_s
    lock = update_transaction._flock(update_transaction._lock_path(state_dir))
    try:
        record = update_transaction.read_journal(state_dir=state_dir)
        if record is None or record.get("transactionId") != transaction_id:
            raise UpdateError("no recorded transaction %s to roll back; nothing to recover"
                              % transaction_id)
        if record["phase"] != "recovery_required":
            raise UpdateError("the transaction is in phase %r, not %r; rollback recovers "
                              "only a failed activation (run recover-abort for a prepared "
                              "one)" % (record["phase"], "recovery_required"))
        revision = record["revision"]
        drain = record.get("drain")
        if not isinstance(drain, dict) or drain.get("revision") != revision \
                or not isinstance(drain.get("capability"), str) or not drain["capability"]:
            raise UpdateError("the transaction does not name the drain it owned; manual "
                              "recovery required")
        _proof_references(record, state_dir)
        update_transaction.journal_recovery_note(transaction_id, state_dir=state_dir,
                                                 recovery="started")
        try:
            status = update_drain.drain_status(base_url, transaction_id, token=token,
                                               revision=revision,
                                               capability=drain["capability"])
        except (update_drain.DrainError, KeyError) as exc:
            if isinstance(exc, update_drain.DrainError) and exc.code == "drain_not_held":
                # a restarted relay owes no hold: the hold is provably gone
                status = None
            else:
                raise UpdateError("the transaction's drain could not be proven; the hold "
                                  "stands and recovery is required: %s" % exc) from exc
        if status is not None and not status.get("quiesced"):
            raise UpdateError("the relay had not quiesced when the rollback was ordered "
                              "(active=%(active)s queued=%(queued)s workflows=%(workflows)s); "
                              "no active work may be interrupted; manual recovery required"
                              % status)
        reverted = update_staging.rollback_transaction_source(record, git=git)
        update_transaction.journal_recovery_note(transaction_id, state_dir=state_dir,
                                                 recovery="source-restored",
                                                 restored=reverted["restored"])
        request = _activated_revision_request(base_url, token)
        # a repeated recovery never repeats a service action: the service is
        # restarted only when the running relay does not already report the
        # exact restored revision through the authenticated endpoint. A relay
        # whose source never moved and one restarted after the restore both
        # report the restored commit already; anything else is restarted once.
        needs_restart = False
        try:
            probe = _authenticated_get(base_url, token, AGENTS_PATH)
        except update_drain.DrainError:
            probe = None  # an unreachable probe is a reason to restart, not a verdict
        if isinstance(probe, dict) and isinstance(probe.get("revision"), dict) \
                and probe["revision"].get("commit") == revision:
            pass
        else:
            needs_restart = True
        if needs_restart:
            restart(service)
            update_transaction.journal_recovery_note(transaction_id, state_dir=state_dir,
                                                     recovery="service-restarted")
            body = update_service.await_revision(revision, authenticated_request=request,
                                                 deadline_s=deadline)
            if body.get("revision", {}).get("commit") != revision:
                raise UpdateError("the restarted relay did not report the restored "
                                  "revision %s; the transaction stays recovery_required" % revision)
        # the barrier held until here; finalize atomically: rolled_back removes it
        update_transaction.transition_locked(transaction_id, "recovery_required",
                                             "rolled_back", state_dir=state_dir,
                                             reason="failed activation rolled back",
                                             restoredRevision=revision,
                                             serviceRestarted=needs_restart)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
    try:
        update_drain.release_drain(base_url, transaction_id, token=token,
                                   revision=revision, capability=drain["capability"])
        hold = "the drain was released with its own capability"
    except update_drain.DrainError as exc:
        if exc.code != "drain_not_held":
            raise UpdateError("the rollback record is durable and the relay reports the "
                              "restored revision %s, but the drain could not be released; "
                              "release it explicitly first: %s" % (revision, exc))
        hold = "the restarted relay owed no drain hold; nothing was released"
    try:
        refresh = _refresh_barrier(base_url, token)
        if refresh.get("admissionClosed"):
            raise update_drain.DrainError(
                "barrier_still_closed",
                "the relay still reports admission closed after the rollback")
    except update_drain.DrainError as exc:
        raise UpdateError("the rollback of %s is durable (phase rolled_back) but the relay "
                          "has not reopened admission (%s); fix the relay and run "
                          "`safe_update recover %s` to retry the refresh"
                          % (transaction_id, exc, transaction_id))
    print("Rolled transaction %s back to the exact prior revision %s, verified through "
          "the authenticated endpoint; %s and admission is open." %
          (transaction_id, revision, hold))
    return {"previous": revision, "restored": reverted["restored"],
            "serviceRestarted": needs_restart}


def cmd_recover(transaction_id, *, base_url=None, inspect=None, restart=None,
                deadline_s=None):
    """Recover a failed activation to the exact, verified prior revision.

    Runs the rollback of this issue's ``recovery_required`` transactions
    (``rollback_transaction``) through the same restricted source/service
    boundaries activation used, and no other. Only this install's recorded
    transaction is recovered: a wrong-device journal is refused by the reader,
    another transaction's id is refused as a replay, and a terminal record is
    finalized read-only — a rolled-back record only retries the one pending
    barrier refresh and is then expunged, never repeating a service action or
    reopening anything twice. Every failed proof keeps the failed state and
    its barrier and names the remedy.
    """
    state_dir = _state_dir()
    record = update_transaction.recover_transaction(state_dir=state_dir)
    if record is None:
        raise UpdateError("no update transaction is recorded for this installation; "
                          "nothing to recover")
    if record["transactionId"] != transaction_id:
        raise UpdateError("the recorded transaction is %s, not %s; refusing to replay "
                          "another transaction's recovery"
                          % (record["transactionId"], transaction_id))
    if record["phase"] == "activated":
        # the activation stands; its only outstanding effect is the admission
        # barrier, whose refresh the original activate could not confirm —
        # expunge the record only once admission is verifiably open, like the
        # rolled_back branch, so the named remedy cannot strand the barrier
        base_url, token = _authorize_drain(base_url)
        try:
            refresh = _refresh_barrier(base_url, token)
            if refresh.get("admissionClosed"):
                raise update_drain.DrainError("barrier_still_closed",
                                              "the relay still reports admission closed")
        except update_drain.DrainError as exc:
            raise UpdateError("the activated record of %s is durable, but the relay has "
                              "not reopened admission (%s); fix the relay and run "
                              "recover again" % (transaction_id, exc)) from exc
        update_transaction.clear_transaction(transaction_id, state_dir=state_dir)
        print("Transaction %s already finished as activated; admission is open and "
              "its durable record was expunged and nothing else was done." % transaction_id)
        return 0
    if record["phase"] == "rolled_back":
        base_url, token = _authorize_drain(base_url)
        try:
            refresh = _refresh_barrier(base_url, token)
            if refresh.get("admissionClosed"):
                raise update_drain.DrainError("barrier_still_closed",
                                              "the relay still reports admission closed")
        except update_drain.DrainError as exc:
            raise UpdateError("the rolled-back record of %s is durable, but the relay has "
                              "not reopened admission (%s); fix the relay and run recover "
                              "again" % (transaction_id, exc)) from exc
        update_transaction.clear_transaction(transaction_id, state_dir=state_dir)
        print("Transaction %s was already rolled back; the relay reopened admission and "
              "the record is expunged." % transaction_id)
        return 0
    if record["phase"] != "recovery_required":
        raise UpdateError("the transaction is in phase %r; recover recovers only a failed "
                          "activation (recovery_required), recover-abort aborts a "
                          "prepared one" % record["phase"])
    base_url, token = _authorize_drain(base_url)
    root = (record.get("inventory") or {}).get("sourceRoot")
    if not root:
        raise UpdateError("the transaction does not name its checkout; manual recovery "
                          "required")
    boundary = inspect if inspect is not None else update_service.inspect_service
    service = boundary(root)
    try:
        rollback_transaction(transaction_id, state_dir=state_dir, service=service,
                             base_url=base_url, token=token, restart=restart,
                             deadline_s=deadline_s)
    except UpdateError as exc:
        raise UpdateError("recovery of %s did not complete (%s); the failed state and "
                          "its barrier are retained; fix the failing step and run "
                          "`safe_update recover %s`" % (transaction_id, exc, transaction_id)) from exc
    update_transaction.clear_transaction(transaction_id, state_dir=state_dir)
    print("Recovery finished: transaction %s is expunged and admission is open." % transaction_id)
    return 0


def _enter_recovery(transaction_id, state_dir, exc):
    """Record a durable recovery_required end for a half-built activation."""
    try:
        update_transaction.transition_locked(transaction_id, update_transaction.read_journal(
            state_dir=state_dir)["phase"], "recovery_required", state_dir=state_dir,
            reason=str(exc))
    except Exception as recovery_exc:  # the journal is retained either way
        raise UpdateError("activation failed (%s) and the recovery record could not be "
                          "written (%s); the barrier still holds admission closed; "
                          "manual recovery required" % (exc, recovery_exc)) from exc
    raise UpdateError("activation failed: %s; the transaction now requires recovery and "
                      "the admission barrier stays closed" % exc)


def _refresh_barrier(base_url, token):
    """Ask the restarted relay to re-read the private barrier state from disk."""
    status, body = update_drain._request("POST", base_url, BARRIER_PATH, token=token)
    if status != 200 or not isinstance(body, dict):
        raise update_drain.DrainError("barrier_refresh_refused_%d" % status,
                                      "the relay refused the barrier refresh: %s"
                                      % (body or {}).get("message", status))
    return body


def _activated_revision_request(base_url, token):
    """An authenticated revision probe: the relay's own exact commit only."""
    def request():
        return _authenticated_get(base_url, token, AGENTS_PATH)
    return request


def _proof_references(record, state_dir):
    """Re-prove the hashed external references a prepared transaction recorded."""
    for point in record.get("state", []):
        path = Path(point["path"])
        try:
            release_identity.private_file_checks(path, "update state reference")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, UpdateError) as exc:
            raise UpdateError("the update state %s changed or vanished: manual recovery "
                              "required: %s" % (path, exc))
        if digest != point.get("digest"):
            raise UpdateError("the update state %s changed since preparation: manual "
                              "recovery required" % path)


def cmd_status():
    record = update_transaction.recover_transaction(state_dir=_state_dir())
    if record is None:
        print("No update transaction is recorded for this installation.")
        return 0
    print("Update transaction %s in phase %s:" % (record["transactionId"], record["phase"]))
    print("  old     exact relay revision %s" % record["revision"])
    print("  target  %s" % record["target"]["identity"].get("tag"))
    print("  stage   %s" % record["stage"]["path"])
    for item in record.get("evidence", []):
        print("  evidence %s at %s" % (item.get("phase"), item.get("at")))
    return 0


def _proof_running_revision(record, base_url):
    """The running relay still runs the exact starting revision the drain names."""
    revision = update_drain._server_revision(base_url)
    if revision != record["revision"]:
        raise UpdateError("the relay runs %s, not the transaction's starting revision %s; "
                          "manual recovery required" % (revision, record["revision"]))


def cmd_recover_abort(transaction_id, *, base_url=None):
    """Abort a prepared transaction: reopen admission only after full proof.

    The proof is the running relay's exact starting revision, the drain it
    still holds for this transaction (proving ownership), the unchanged
    source, stage and hashed state references, and a phase that shows no
    activation has occurred yet. Anything else is a refusal for manual
    recovery; nothing is reopened or quietly expunged.
    """
    state_dir = _state_dir()
    record = update_transaction.recover_transaction(state_dir=state_dir)
    if record is None:
        raise UpdateError("no update transaction is recorded; nothing to abort")
    if record["transactionId"] != transaction_id:
        raise UpdateError("the recorded transaction is %s, not %s; refusing to replay another "
                          "transaction's abort" % (record["transactionId"], transaction_id))
    if record["phase"] not in ("prepared", "draining"):
        raise UpdateError("the transaction is in phase %r: manual recovery required"
                          % record["phase"])
    base_url, token = _authorize_drain(base_url)
    # Prove before any effect: unchanged external state, the exact starting
    # revision still running, this transaction's owned drain and its quiescence.
    _proof_references(record, state_dir)
    _proof_running_revision(record, base_url)
    drain = record.get("drain")
    revision = record["revision"]
    if not isinstance(drain, dict) or drain.get("revision") != revision:
        raise UpdateError("the transaction does not name the drain it owned; manual recovery required")
    try:
        status = update_drain.drain_status(base_url, transaction_id, token=token,
                                          revision=revision, capability=drain["capability"])
    except (update_drain.DrainError, KeyError) as exc:
        # A restarted relay owes no hold: drain_not_held proves this recorded hold no
        # longer keeps admission closed, so there is nothing left to release. Every
        # other refusal is not evidence; manual recovery stays required.
        if not (isinstance(exc, update_drain.DrainError) and exc.code == "drain_not_held"):
            raise UpdateError("the drain of this transaction could not be proven; the hold "
                              "stands and manual recovery is required: %s" % exc) from exc
        status = None
    if status is not None and not status.get("quiesced"):
        raise UpdateError("the relay had not quiesced when the abort was ordered "
                          "(active=%(active)s queued=%(queued)s workflows=%(workflows)s); "
                          "the drain waited: manual recovery required" % status)
    record = update_transaction.transition(transaction_id, record["phase"], "rolled_back",
                                           state_dir=state_dir, reason="operator recover-abort")
    if status is None:
        update_transaction.clear_transaction(transaction_id, state_dir=state_dir)
        print("Transaction %s aborted; the relay hold ended without an explicit release, so "
              "admission reopens after the proof only. Nothing was ever activated."
              % transaction_id)
        return 0
    try:
        update_drain.release_drain(base_url, transaction_id, token=token,
                                   revision=revision, capability=drain["capability"])
    except (update_drain.DrainError, KeyError) as exc:
        raise UpdateError("the abort record is written and admission reopens, but the drain "
                          "could not be released; release it explicitly first: %s" % exc)
    update_transaction.clear_transaction(transaction_id, state_dir=state_dir)
    print("Transaction %s aborted; the drain was released with its capability and admission "
          "reopens. Nothing was ever activated." % transaction_id)
    return 0


def cmd_consume(tag):
    identity, _manifest, _bytes = _resolve(tag)
    consent = release_identity.consume_consent(identity, state_dir=_state_dir(), now=time.time())
    print("Consent validated and consumed for %s at commit %s." % (consent["tag"], consent["commit"]))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Inspect a published agent-relay release, record device-local "
                    "approval or drive exactly verified activation; never pulls or "
                    "installers, never a force mode.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name, handler, help_text in (
        ("inspect", cmd_inspect, "resolve and validate a stable release identity (metadata only)"),
        ("approve", cmd_approve, "interactively record a private, expiring device-local consent"),
        ("consume", cmd_consume, "validate and burn the recorded consent for a release"),
    ):
        sub = subcommands.add_parser(name, help=help_text)
        sub.add_argument("tag", help="exact release tag to %s, e.g. 1.2.3" % name)
    stage = subcommands.add_parser(
        "stage", help="build a verified, isolated stage of an approved release; "
                      "installs nothing and executes nothing")
    stage.add_argument("tag", help="exact release tag to stage, e.g. 1.2.3")
    stage.add_argument("--install-dir", required=True,
                       help="this installation's checkout directory that approval was "
                            "recorded for; it is inspected and never written")
    drain = subcommands.add_parser(
        "drain", help="rehearse the safe update's shared-lock drain: verify the "
                      "stage and consent, hold the relay's drain, wait boundedly for "
                      "quiescence and release; installs nothing")
    drain.add_argument("tag", help="exact release tag the verified stage is of, e.g. 1.2.3")
    drain.add_argument("--install-dir", required=True,
                       help="this installation's checkout the stage was staged from")
    drain.add_argument("--base-url", default=None,
                       help="the running relay to drain; default AGENT_RELAY_URL or "
                            "A2A_RELAY_URL")
    drain.add_argument("--transaction-id", default=None,
                       help="an explicit name for this update transaction")
    prepare = subcommands.add_parser(
        "prepare-activation", help="validate the approved release, stage and checkout, "
        "hold the exact-revision drain and durably prepare a recoverable activation "
        "transaction; installs nothing, activates nothing and restarts nothing")
    prepare.add_argument("tag", help="exact release tag the verified stage is of, e.g. 1.2.3")
    prepare.add_argument("--install-dir", required=True,
                         help="this installation's checkout the stage was staged from")
    prepare.add_argument("--base-url", default=None,
                         help="the running relay to drain; default AGENT_RELAY_URL or "
                              "A2A_RELAY_URL")
    prepare.add_argument("--transaction-id", default=None,
                         help="an explicit name for this update transaction")
    activate = subcommands.add_parser(
        "activate", help="activate the exact staged release of a prepared transaction "
        "under the transaction's own drain: swap the verified source, restart the "
        "positively validated per-user service barrier boundary and verify the "
        "restarted relay; anything uncertain stays blocked for explicit recovery")
    activate.add_argument("transaction_id", metavar="TRANSACTION-ID",
                          help="the prepared transaction whose exact stage is activated")
    activate.add_argument("--install-dir", required=True,
                          help="this installation's checkout the service definition "
                               "must positively name")
    activate.add_argument("--base-url", default=None,
                          help="the running relay to drain and verify; default "
                               "AGENT_RELAY_URL or A2A_RELAY_URL")
    subcommands.add_parser(
        "status", help="print the recorded update transaction of this installation, if any")
    recover = subcommands.add_parser(
        "recover", help="recover a failed activation (recovery_required) to the exact, "
        "journaled prior revision through the same restricted source/service "
        "boundary; anything uncertain stays recovery_required with its barrier closed")
    recover.add_argument("transaction_id", metavar="TRANSACTION-ID",
                         help="the failed transaction to roll back")
    recover.add_argument("--base-url", default=None,
                         help="the running relay to prove and release; default "
                              "AGENT_RELAY_URL or A2A_RELAY_URL")
    recover = subcommands.add_parser(
        "recover-abort", help="abort a prepared activation transaction after proving the "
        "original revision, unchanged source and state and that no activation occurred; "
        "anything else is refused for manual recovery")
    recover.add_argument("transaction_id", metavar="TRANSACTION-ID",
                         help="the exact transaction to abort")
    recover.add_argument("--base-url", default=None,
                         help="the running relay to prove and release; default "
                              "AGENT_RELAY_URL or A2A_RELAY_URL")
    args = parser.parse_args(argv)
    handlers = {"inspect": cmd_inspect, "approve": cmd_approve, "consume": cmd_consume}
    try:
        if args.command == "stage":
            return cmd_stage(args.tag, args.install_dir)
        if args.command == "drain":
            return cmd_drain(args.tag, args.install_dir, base_url=args.base_url,
                             transaction_id=args.transaction_id)
        if args.command == "prepare-activation":
            return cmd_prepare_activation(args.tag, args.install_dir, base_url=args.base_url,
                                          transaction_id=args.transaction_id)
        if args.command == "activate":
            return cmd_activate(args.transaction_id, args.install_dir,
                                base_url=args.base_url)
        if args.command == "status":
            return cmd_status()
        if args.command == "recover":
            return cmd_recover(args.transaction_id, base_url=args.base_url)
        if args.command == "recover-abort":
            return cmd_recover_abort(args.transaction_id, base_url=args.base_url)
        return handlers[args.command](args.tag)
    except UpdateError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print("error: update state could not be used: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
