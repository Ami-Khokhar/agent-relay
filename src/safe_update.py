#!/usr/bin/env python3
"""Safe update CLI: inspect a published release and record device-local consent.

This is the first consumer of ``release_identity``. ``inspect`` resolves and
validates a stable release identity (metadata only), ``approve`` adds an
interactive, private, 15-minute device-local consent record, and ``stage``
builds an isolated private repository holding the exact approved commit —
only for a positively identified clean canonical source checkout, only
while the recorded consent is still valid, and never by executing any
staged content. Nothing is installed or activated by any of these
commands; later stages will extend the same CLI to consume the verified
stage.
"""

import argparse
import http.client
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

import hashlib

import release_identity
import update_drain
import update_staging
import update_transaction
from release_identity import UpdateError

STATE_LABEL = "release update consent"
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
    identity, manifest, _bytes = release_identity.resolve_release(tag, fetch_json=_fetch)
    return identity, manifest


def _state_dir():
    state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state_home) / "agent-relay"


def cmd_inspect(tag):
    identity, manifest = _resolve(tag)
    print("Release identity validated (no release will be installed):")
    print(release_identity.identity_summary(identity))
    print("Manifest schema: %s" % manifest["schema"])
    return 0


def cmd_approve(tag):
    identity, _manifest = _resolve(tag)
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
    record = update_transaction.begin_transaction(
        {**receipt, "transactionId": transaction_id, "drain": acquired},
        state_dir=state_dir)
    print("Prepared activation transaction %s:" % transaction_id)
    print("  target  %s (%s)" % (identity["tag"], identity["commit"]))
    print("  old     exact relay revision %s" % acquired["revision"])
    print("  stage   %s" % receipt["stage"])
    print("The exact-revision drain is held and admission is closed; nothing was activated"
          " or restarted. Use status and recover-abort to inspect or abort it.")
    return 0


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
        raise UpdateError("the drain of this transaction could not be proven; the hold stands "
                          "and manual recovery is required: %s" % exc)
    if not status.get("quiesced"):
        raise UpdateError("the relay had not quiesced when the abort was ordered "
                          "(active=%(active)s queued=%(queued)s workflows=%(workflows)s); "
                          "the drain waited: manual recovery required" % status)
    record = update_transaction.transition(transaction_id, record["phase"], "rolled_back",
                                           state_dir=state_dir, reason="operator recover-abort")
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
    identity, _manifest = _resolve(tag)
    consent = release_identity.consume_consent(identity, state_dir=_state_dir(), now=time.time())
    print("Consent validated and consumed for %s at commit %s." % (consent["tag"], consent["commit"]))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Inspect a published agent-relay release or record device-local approval; never installs or activates.")
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
    subcommands.add_parser(
        "status", help="print the recorded update transaction of this installation, if any")
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
        if args.command == "status":
            return cmd_status()
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
