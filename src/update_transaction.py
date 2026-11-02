#!/usr/bin/env python3
"""Activation transaction journal and the private admission barrier (#119, #157).

One safe update is one durable, recoverable transaction. ``begin_transaction``
records a prepared transaction (the verified stage, the exact old commit, the
drain ownership, hashed references to the external state this update relies
on) in a private no-follow atomic fsync journal, and ``transition`` performs
the phase compare-and-swap. Between the prepare and its finish, a second
private record — the admission barrier — holds every relay of this
installation closed to new external admissions: the server reads the state
(through ``admission_blocker``) at startup and refuses new work while a
transaction is live, so a crash can never silently reopen admission and MCP
autostart can never admit work inside an activation gap.

Fail-closed rules: a journal or barrier that is missing, malformed, torn,
stale (barrier present without a live transaction) or foreign (bound to
another device/install) refuses admission and is never silently ignored or
rewritten; only ``recover-abort`` reopens, and only after proving the original
revision, unchanged source/state and that no activation occurred. Every write
is private (0600/0700, no symlinks), fsynced and atomic (temp file + rename),
so a crash at any point leaves the previous complete record or the new one,
never a torn one. One transaction at a time per install: a live journal
refuses any second ``begin_transaction``, and the write lock refuses a
concurrent process.
"""
from __future__ import annotations

try:
    import fcntl
except ImportError:  # non-POSIX: importing server.py must keep working there
    fcntl = None
import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path

from release_identity import (
    UpdateError,
    private_file_checks,
    private_state_dir,
    strict_json,
)

TRANSACTION_SCHEMA = "agent-relay.update-transaction/v1"
BARRIER_SCHEMA = "agent-relay.update-barrier/v1"
MAX_RECORD_BYTES = 256 * 1024
TRANSACTION_RE = re.compile(r"[A-Za-z0-9_.~-]{1,128}$")
COMMIT_RE = re.compile(r"[0-9a-f]{40}$")
DEVICE_RE = re.compile(r"[0-9a-f]{32}$")
# 40-hex any-HexCast of a sha256, used for the hashed external references.
DIGEST_RE = re.compile(r"[0-9a-f]{64}$")
# The lifecycle phases. The consumers through #157 introduced "prepared" and
# "draining"; #158's activation walks a journaled transaction through
# "quiescent", "activating" and "restarted" to a terminal end.
PHASES = ("prepared", "draining", "quiescent", "activating", "restarted",
          "activated", "recovery_required", "rolled_back")
# A transaction in these phases is finished as far as the journal is concerned:
# it still refuses admission until its barrier (recovery_required) is removed
# or its record is expunged.
TERMINAL_PHASES = ("activated", "recovery_required", "rolled_back")
# Only these end phases may have their admission barrier removed; a
# recovery_required transaction keeps its barrier so admission stays closed
# until explicit manual recovery proves the original state.
BARRIER_FREE_PHASES = ("rolled_back", "activated")
UPDATE_DIRNAME = "update"
JOURNAL_NAME = "transaction.json"
BARRIER_NAME = "admission-barrier.json"
LOCK_NAME = "transaction.lock"


def _update_dir(state_dir):
    """The private, real, user-owned update state directory."""
    return private_state_dir(Path(state_dir) / UPDATE_DIRNAME, "update state")


def _journal_path(state_dir):
    return _update_dir(state_dir) / JOURNAL_NAME


def _barrier_path(state_dir):
    return _update_dir(state_dir) / BARRIER_NAME


def _lock_path(state_dir):
    return _update_dir(state_dir) / LOCK_NAME


def _flock(path):
    """An exclusive, non-blocking, no-follow lock; refusing a concurrent process."""
    if fcntl is None:
        raise UpdateError("the update journal needs POSIX file locking; refusing to "
                          "update state where os.flock is unavailable")
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise UpdateError("cannot open the transaction lock; refusing a concurrent process: %s"
                          % exc) from exc
    try:
        info = path.lstat()
        owner = getattr(os, "geteuid", None)
        if (owner is not None and info.st_uid != owner()) or path.is_symlink() \
                or not stat.S_ISREG(info.st_mode):
            raise UpdateError("the transaction lock is not a private regular file; "
                              "refusing unsafe state")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise UpdateError(
                "another updater process owns the transaction state; refusing a "
                "concurrent process: %s" % exc) from exc
        return fd
    except BaseException:
        os.close(fd)
        raise


def _atomic_write(path, payload):
    """Private, atomic, fsynced write: a crash leaves the old or the new bytes."""
    tmp = Path(path.parent, "%s.%s.tmp" % (path.name, os.urandom(8).hex()))
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise UpdateError("cannot write update state; check permissions and free space: %s"
                          % exc) from exc
    try:
        os.fchmod(fd, 0o600)
        data = memoryview(payload)
        while data:
            data = data[os.write(fd, data):]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        try:
            os.replace(str(tmp), str(path))
        except OSError as exc:
            raise UpdateError("cannot save update state; check permissions and free space "
                              "(a crash left the previous record): %s" % exc) from exc
        _fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _fsync_dir(path):
    """Best-effort directory fsync so renames and removals survive a crash."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def install_fingerprint(state_dir):
    """A device-and-install binding: the private device id plus the install root."""
    device = _device_id(state_dir)
    root = hashlib.sha256(str(Path(state_dir).resolve()).encode("utf-8")).hexdigest()
    return {"device": device, "root": root}


def _device_id(state_dir):
    """This installation's private device id (never written into shared output)."""
    path = Path(state_dir) / "device-id"
    try:
        private_file_checks(path, "device id")
        device = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise UpdateError("no device id in %s; approve or stage this release here first" % path) \
            from exc
    except UpdateError as exc:
        raise UpdateError("the device id is unusable: %s" % exc)
    if not DEVICE_RE.fullmatch(device):
        raise UpdateError("device id is corrupt; refusing unsafe promotion state")
    return device


def _check_shape(record, label, schema, *, need_binding=True):
    """The strict record shape every reader trusts; anything else fails closed."""
    if not isinstance(record, dict):
        raise UpdateError("%s is not a record object; refusing unsafe state" % label)
    if record.get("schema") != schema:
        raise UpdateError("%s schema is not %s; refusing unsafe state" % (label, schema))
    transaction = record.get("transactionId")
    if not isinstance(transaction, str) or not TRANSACTION_RE.fullmatch(transaction):
        raise UpdateError("%s names no well-formed transaction; refusing unsafe state" % label)
    phase = record.get("phase")
    if not isinstance(phase, str) or (phase not in PHASES and phase not in TERMINAL_PHASES):
        raise UpdateError("%s names no known phase %r; refusing unsafe state" % (label, phase))
    revision = record.get("revision")
    if not isinstance(revision, str) or not COMMIT_RE.fullmatch(revision or ""):
        raise UpdateError("%s names no exact starting revision; refusing unsafe state" % label)
    binding = record.get("binding")
    if need_binding and (not isinstance(binding, dict) or not isinstance(binding.get("device"), str)
                         or not isinstance(binding.get("root"), str)
                         or not DIGEST_RE.fullmatch(binding.get("root") or "")):
        raise UpdateError("%s is not bound to a device and install root" % label)
    return record


def _read(path, label):
    if not path.exists():
        return None
    try:
        private_file_checks(path, label)
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UpdateError("cannot read the %s; refusing unsafe state: %s" % (label, exc)) from exc
    if len(raw) > MAX_RECORD_BYTES:
        raise UpdateError("the %s exceeds %d bytes; refusing unsafe state" % (label, MAX_RECORD_BYTES))
    return strict_json(raw, label)


def read_journal(state_dir):
    """The raw transaction journal record, or None. Malformed records raise."""
    return _read(_journal_path(state_dir), "transaction journal")


def recover_transaction(*, state_dir):
    """The recoverable transaction of this install, or None; torn/foreign fails closed."""
    record = _read(_journal_path(state_dir), "transaction journal")
    if record is not None:
        _check_shape(record, "transaction journal", TRANSACTION_SCHEMA)
        if record["binding"] != install_fingerprint(state_dir):
            raise UpdateError(
                "the transaction journal was written for a different device or install; "
                "refusing unsafe state")
    return record


def _barrier_shape(barrier, label):
    return _check_shape(barrier, label, BARRIER_SCHEMA, need_binding=False)


def read_barrier(state_dir):
    """The admission barrier of this install, or None. Malformed records raise."""
    directory = _update_dir(state_dir)
    path = directory / BARRIER_NAME
    if not path.exists():
        return None
    try:
        private_file_checks(path, "admission barrier")
        raw = path.read_bytes()
    except OSError as exc:
        raise UpdateError("cannot read the admission barrier; refusing unsafe state: %s" % exc) \
            from exc
    if len(raw) > MAX_RECORD_BYTES:
        raise UpdateError("the admission barrier exceeds %d bytes; refusing unsafe state" % MAX_RECORD_BYTES)
    return _barrier_shape(strict_json(raw, "admission barrier"), "admission barrier")


def _write_barrier(state_dir, barrier):
    _atomic_write(_barrier_path(state_dir), json.dumps(barrier, indent=1).encode("utf-8"))
    os.chmod(_barrier_path(state_dir), 0o600)


def _remove_barrier(state_dir):
    path = _barrier_path(state_dir)
    try:
        os.unlink(str(path))
    except FileNotFoundError:
        return
    _fsync_dir(_update_dir(state_dir))


def admission_blocker(*, state_dir, running_revision=None):
    """A refusal for this install's update state, or None when admission is open.

    Fail-closed on every inconsistency: a journal or barrier that cannot be
    read strictly, a barrier without a live transaction (stale), a foreign
    device binding or an empty, non-40-hex revision.

    The refusal's ``transactionId`` is None only when the state itself could
    not be trusted enough to name one.
    """
    record = None
    barrier = None
    try:
        record = read_journal(state_dir)
        if record is not None:
            # A valid-JSON but incomplete record would KeyError instead of
            # UpdateError; shape-check it here so admission fails closed.
            record = _check_shape(record, "transaction journal", TRANSACTION_SCHEMA)
        barrier = read_barrier(state_dir)
    except UpdateError as exc:
        return {"transactionId": None, "reason": str(exc)}
    if record is not None and record["binding"] != install_fingerprint(state_dir):
        return {"transactionId": record.get("transactionId"),
                "reason": "the transaction journal was written for another device or install"}
    if barrier is not None and barrier.get("binding") != install_fingerprint(state_dir):
        return {"transactionId": barrier.get("transactionId"),
                "reason": "the admission barrier was written for another device or install"}
    if running_revision is not None and barrier is not None and barrier["revision"] != running_revision:
        return {"transactionId": barrier["transactionId"],
                "reason": "the admission barrier binds revision %s, not the revision this "
                          "relay runs; recover the transaction before serving work"
                          % barrier["revision"]}
    if record is not None and record["phase"] not in TERMINAL_PHASES:
        return {"transactionId": record["transactionId"],
                "reason": "an update transaction is live and activation is unfinished "
                          "in %r" % record["phase"]}
    if barrier is not None:
        if record is None:
            return {"transactionId": barrier.get("transactionId"),
                    "reason": "a stale admission barrier is present with no transaction"}
        return {"transactionId": barrier.get("transactionId"),
                "reason": "a stale admission barrier for a finished transaction "
                          "(phase %r) is present; unsure it is stale, refusing" % record["phase"]}
    return None


def _write_journal(state_dir, record):
    """Write one transaction record: journal first, then the barrier it commands."""
    _atomic_write(_journal_path(state_dir), json.dumps(record, indent=1).encode("utf-8"))
    os.chmod(_journal_path(state_dir), 0o600)
    if record["phase"] in BARRIER_FREE_PHASES:
        _remove_barrier(state_dir)
    else:
        _write_barrier(state_dir, {
            "schema": BARRIER_SCHEMA,
            "transactionId": record["transactionId"],
            "phase": record["phase"],
            "revision": record["revision"],
            "binding": record["binding"],
        })


def _external_references(receipt, state_dir):
    """Hashed references to the external state this update relies on; never contents."""
    references = []
    for point in (Path(state_dir) / "consent.json",
                  Path(receipt["stage"]) / "stage-receipt.json"):
        try:
            digest = hashlib.sha256(point.read_bytes()).hexdigest()
        except OSError as exc:
            raise UpdateError("cannot hash the update state at %s: %s" % (point, exc)) from exc
        references.append({"path": str(point), "digest": digest})
    return references


def begin_transaction(receipt, *, state_dir):
    """Record one prepared, recoverable activation transaction for this install.

    ``receipt`` is the verified stage receipt extended with this release's
    committing facts: ``transactionId`` and ``drain`` (the exact starting
    ``revision`` and the relay-issued opaque ``capability``). Nothing is
    prepared while any other transaction of this install is recorded; the
    journal record binds the old commit, the target identity, the stage,
    the hashed external references and the drain ownership, and the
    admission barrier is written with it.
    """
    if not isinstance(receipt, dict):
        raise UpdateError("a begin_transaction receipt must be a record of the verified stage")
    digest = _check_receipt(receipt)
    lane = _update_dir(state_dir)
    fd = _flock(_lock_path(state_dir))
    try:
        record = read_journal(state_dir)
        if record is not None:
            _check_shape(record, "transaction journal", TRANSACTION_SCHEMA)
            if record["phase"] not in TERMINAL_PHASES:
                raise UpdateError(
                    "transaction %s is already live in %r; one install holds one update "
                    "transaction at a time" % (record["transactionId"], record["phase"]))
            raise UpdateError(
                "transaction %s is still recorded (%s); recover-abort it before preparing "
                "another" % (record["transactionId"], record["phase"]))
        drain = receipt.get("drain")
        revision = drain.get("revision") if isinstance(drain, dict) else None
        transaction = receipt.get("transactionId")
        if not isinstance(transaction, str) or not TRANSACTION_RE.fullmatch(transaction):
            raise UpdateError("the receipt names no well-formed transactionId")
        if not isinstance(revision, str) or not COMMIT_RE.fullmatch(revision):
            raise UpdateError(
                "the receipt names no exact starting revision from the running relay")
        if not isinstance(drain.get("capability"), str) or len(drain["capability"]) < 32:
            raise UpdateError(
                "the receipt carries no relay-issued drain capability; the hold cannot be "
                "recovered or released safely")
        now = time.time()
        record = {
            "schema": TRANSACTION_SCHEMA,
            "transactionId": transaction,
            "phase": "prepared",
            "revision": revision,
            "target": {"identity": receipt["identity"], "identityDigest": digest},
            "stage": {"path": receipt["stage"],
                      "inventory": receipt.get("inventory", [])},
            "inventory": {"sourceRoot": receipt.get("sourceRoot"),
                          "sourceCommit": receipt.get("sourceCommit")},
            "state": _external_references(receipt, state_dir),
            "drain": drain,
            "binding": install_fingerprint(state_dir),
            "createdAt": now,
            "updatedAt": now,
            "evidence": [],
        }
        _write_journal(state_dir, record)
        return record
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _check_receipt(receipt):
    """The stage receipt the transaction binds; every required block is proven."""
    identity = receipt.get("identity")
    digest = receipt.get("identityDigest")
    if not isinstance(identity, dict) or not isinstance(digest, str):
        raise UpdateError("the receipt does not bind a release identity")
    for key in ("stage", "sourceRoot", "sourceCommit"):
        value = receipt.get(key)
        if not isinstance(value, str) or not value:
            raise UpdateError("the stage receipt does not name its %s" % key)
    return digest


def transition(transaction_id, expected_phase, next_phase, *, state_dir, **evidence):
    """The only one-way phase compare-and-swap; refuses a replay or a wrong phase.

    ``evidence`` is stored with the transition (at, what happened); only
    fields that were explicitly passed are written. A phase with no consumer
    in this issue is refused, so a caller cannot advance a half-built state
    by conjuring a phase name.
    """
    if not isinstance(transaction_id, str) or not TRANSACTION_RE.fullmatch(transaction_id):
        raise UpdateError("transactionId must name a well-formed transaction")
    if next_phase not in PHASES or next_phase == expected_phase:
        raise UpdateError(
            "phase %r has no consumer in this issue or is not a next phase; refusing "
            "a state built by conjuring a phase name" % next_phase)
    index = PHASES.index(expected_phase) if expected_phase in PHASES else -1
    if index == -1:
        raise UpdateError("phase %r is not a live phase of this issue; refusing" % expected_phase)
    if PHASES.index(next_phase) <= index:
        raise UpdateError("a transaction moves forward through phases; refusing "
                          "a repeat %s=%s transaction" % (expected_phase, next_phase))
    lane = _update_dir(state_dir)
    fd = _flock(_lock_path(state_dir))
    try:
        record = read_journal(state_dir)
        if record is None:
            raise UpdateError("no transaction is recorded in %s; nothing to transition" % lane)
        _check_shape(record, "transaction journal", TRANSACTION_SCHEMA)
        if record["binding"] != install_fingerprint(state_dir):
            raise UpdateError("the transaction journal was written for another device or "
                              "install; refusing unsafe state")
        if record["transactionId"] != transaction_id:
            raise UpdateError(
                "the transaction journal is for %s, not %s; refusing to replay another "
                "transaction's transition" % (record["transactionId"], transaction_id))
        if record["phase"] != expected_phase:
            raise UpdateError(
                "the transaction is in %r, not the expected %r; refusing an out-of-order "
                "transition" % (record["phase"], expected_phase))
        record = transition_locked(record["transactionId"], record["phase"], next_phase,
                                   state_dir=state_dir, **evidence)
        return record
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def transition_locked(transaction_id, expected_phase, next_phase, *, state_dir, **evidence):
    """The phase compare-and-swap for a caller that already holds the lock.

    ``transition`` itself takes the transaction lock because most consumers are
    one-call scripts; the activation flow rechecks and effects many phases and
    one durable boundary per write, so it must hold the lock across them and
    loop here instead — never re-entering _flock, which would deadlock against
    its own held lock).
    """
    if not isinstance(transaction_id, str) or not TRANSACTION_RE.fullmatch(transaction_id):
        raise UpdateError("transactionId must name a well-formed transaction")
    if next_phase not in PHASES or next_phase == expected_phase:
        raise UpdateError(
            "phase %r has no consumer in this issue or is not a next phase; refusing "
            "a state built by conjuring a phase name" % next_phase)
    index = PHASES.index(expected_phase) if expected_phase in PHASES else -1
    if index == -1:
        raise UpdateError("phase %r is not a live phase of this issue; refusing" % expected_phase)
    if PHASES.index(next_phase) <= index:
        raise UpdateError("a transaction moves forward through phases; refusing "
                          "a repeat %s=%s transaction" % (expected_phase, next_phase))
    record = read_journal(state_dir)
    if record is None:
        raise UpdateError("no transaction is recorded; nothing to transition")
    _check_shape(record, "transaction journal", TRANSACTION_SCHEMA)
    if record["binding"] != install_fingerprint(state_dir):
        raise UpdateError("the transaction journal was written for another device or "
                          "install; refusing unsafe state")
    if record["transactionId"] != transaction_id:
        raise UpdateError(
            "the transaction journal is for %s, not %s; refusing to replay another "
            "transaction's transition" % (record["transactionId"], transaction_id))
    if record["phase"] != expected_phase:
        raise UpdateError(
            "the transaction is in %r, not the expected %r; refusing an out-of-order "
            "transition" % (record["phase"], expected_phase))
    record["phase"] = next_phase
    now = time.time()
    record["updatedAt"] = now
    record["evidence"].append({"phase": next_phase, "at": now, **evidence})
    _write_journal(state_dir, record)
    return record


def journal_recovery_note(transaction_id, *, state_dir, **evidence):
    """Append one recovery evidence entry to the recorded recovery_required
    journal without moving its phase.

    ``rollback_transaction`` holds the lock and notes every durable recovery
    step here *before* the next effect, so a crash mid-recovery leaves a
    journal that shows exactly how far the last recovery got and no step is
    silently repeated or skipped by the next recovery attempt. The phase and
    the admission barrier stay exactly as they were.
    """
    lane = _update_dir(state_dir)
    record = read_journal(state_dir)
    if record is None:
        raise UpdateError("no transaction is recorded in %s; nothing to note" % lane)
    _check_shape(record, "transaction journal", TRANSACTION_SCHEMA)
    if record["binding"] != install_fingerprint(state_dir):
        raise UpdateError("the transaction journal was written for another device or "
                          "install; refusing unsafe state")
    if record["transactionId"] != transaction_id:
        raise UpdateError("the transaction journal is for %s, not %s"
                          % (record["transactionId"], transaction_id))
    record["evidence"].append({"phase": record["phase"], "at": time.time(),
                              "kind": "recovery", **evidence})
    _write_journal(state_dir, record)
    return record


def clear_transaction(transaction_id, *, state_dir):
    """Expunge a finished transaction record after a proven, checked end.

    Only a terminal-phase journal of this install for exactly this id is
    removed; a live transaction is refused, so an abort can never forget
    its own proof by clearing itself early.
    """
    if not isinstance(transaction_id, str) or not TRANSACTION_RE.fullmatch(transaction_id):
        raise UpdateError("transactionId must name a well-formed transaction")
    _update_dir(state_dir)
    fd = _flock(_lock_path(state_dir))
    try:
        record = read_journal(state_dir)
        if record is None:
            return None
        _check_shape(record, "transaction journal", TRANSACTION_SCHEMA)
        if record["binding"] != install_fingerprint(state_dir):
            raise UpdateError("the transaction journal was written for another device or "
                              "install; refusing unsafe state")
        if record["transactionId"] != transaction_id:
            raise UpdateError("the transaction journal is for %s, not %s"
                              % (record["transactionId"], transaction_id))
        if record["phase"] not in TERMINAL_PHASES:
            raise UpdateError("the transaction is live in %r; it can only be expunged after "
                              "a proven end" % record["phase"])
        os.unlink(str(_journal_path(state_dir)))
        _fsync_dir(_update_dir(state_dir))
        return record
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
