"""The activation transaction journal, admission barrier and their consumers (#119, #157).

Durability: every crash point around the journal and barrier writes leaves a
complete record or nothing, never a torn one, and readers refuse what they
cannot strictly trust. The relay closes admission across a restart while a
transaction is live, and reopens it only through the proven abort. The CLI's
prepare-activation revalidates the release, stage, checkout and consent at the
effect boundary; preparing and aborting never change the active source, config,
device state or the relay process.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import release_identity
import release_identity as identity
import update_drain
import update_staging
import update_transaction
import safe_update
from relay_helpers import TOKEN, Relay

TAG = "0.4.0"
NOW = 1_770_000_000.0
STAMP = "2026-10-01T00:00:00Z"
ORIGIN = "https://github.com/Ami-Khokhar/agent-relay.git"
OLD_COMMIT = "b" * 40
CAPABILITY = "c" * 64
TXN = "txn-ok"
AGENT = {"id": "worker", "command": sys.executable,
         "args": ["-c", "import sys; print(sys.stdin.read())"]}
BASE_URL = "127.0.0.1-fake"


def _run_git(root, *args):
    # Author identity and config isolation are explicit: a developer's or CI
    # runner's global git config must never change what the fixture commits.
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "f", "GIT_AUTHOR_EMAIL": "f@example.com",
           "GIT_COMMITTER_NAME": "f", "GIT_COMMITTER_EMAIL": "f@example.com"}
    result = subprocess.run(["/usr/bin/git", "-C", str(root), *args], capture_output=True,
                            text=True, env=env)
    if result.returncode != 0:
        raise AssertionError("fixture git %s failed: %s" % (args, result.stderr))
    return result.stdout


def _tree_inventory(root):
    """The full byte inventory of a tree: the unchanged-active-data proof."""
    inventory = {}
    for dirpath, dirnames, filenames in os.walk(str(root)):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            info = path.lstat()
            payload = b"" if path.is_dir() else path.read_bytes()
            inventory[path.relative_to(root).as_posix()] = (
                stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode),
                len(payload), hashlib.sha256(payload).hexdigest())
    return inventory


class _FakeTerminal:
    def __init__(self, answers):
        self.answers = list(answers)

    def is_tty(self):
        return True

    def prompt(self, message):
        return self.answers.pop(0) if self.answers else ""

    def echo(self, message):
        pass


class _FakeRelay:
    """A recording drain server: only the real admin verbs are ever called."""

    def __init__(self, revision=OLD_COMMIT, capability=CAPABILITY):
        self.revision, self.capability = revision, capability
        self.calls, self.releases = [], []

    def acquire(self, _base, transaction_id, *, token=None, wait_s=None):
        self.calls.append(("acquire", transaction_id, token))
        return {"transactionId": transaction_id, "revision": self.revision,
                "capability": self.capability, "quiesced": False,
                "active": 1, "queued": 0, "workflows": 0}

    def status(self, _base, transaction_id, *, token=None, revision=None,
               capability=None, timeout=None):
        self.calls.append(("status", transaction_id, revision, capability))
        if revision != self.revision or capability != self.capability:
            raise update_drain.DrainError("revision_mismatch", "refused")
        return {"quiesced": True, "active": 0, "queued": 0, "workflows": 0,
                "transactionId": transaction_id}

    def release(self, _base, transaction_id, *, token=None, revision=None,
                capability=None, timeout=None):
        self.calls.append(("release", transaction_id, revision, capability))
        self.releases.append(transaction_id)
        if revision != self.revision or capability != self.capability:
            raise update_drain.DrainError("revision_mismatch", "refused")
        return {"released": True, "transactionId": transaction_id}

    def revision_of(self, _base):
        self.calls.append(("healthz",))
        return self.revision


class _Fixture:
    """One clean canonical checkout, one private state dir, one consumed consent."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.state = base / "state"
        self.repo = base / "relay-checkout"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "README.md").write_text("# agent-relay\n")
        (self.repo / "src" / "safe_update.py").write_text("# updater\n")
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "add", "-A")
        _run_git(self.repo, "commit", "-q", "-m", "release")
        self.commit = _run_git(self.repo, "rev-parse", "HEAD").strip()
        _run_git(self.repo, "remote", "add", "origin", ORIGIN)
        self.install_patch = patch.object(identity, "installation_path",
                                          lambda: self.repo.resolve())
        self.install_patch.start()
        files = [{"path": "README.md", "mode": "100644",
                  "sha256": hashlib.sha256((self.repo / "README.md").read_bytes()).hexdigest()},
                 {"path": "src/safe_update.py", "mode": "100644",
                  "sha256": hashlib.sha256((self.repo / "src" / "safe_update.py")
                                           .read_bytes()).hexdigest()}]
        self.manifest = {"schema": identity.MANIFEST_SCHEMA,
                         "repository": identity.REPOSITORY, "version": TAG, "tag": TAG,
                         "commit": self.commit, "files": files}
        self.manifest_bytes = json.dumps(self.manifest).encode("utf-8")
        self.stage_dir = base / "stage-0.4.0"
        self.stage_dir.mkdir()
        self.stage_receipt = self.stage_dir / "stage-receipt.json"
        self._write_stage_receipt()
        self.state.mkdir(mode=0o700)
        # the durable single-use consent record: approved, then consumed by staging,
        # exactly as the earlier issues leave it for a prepared activation
        identity.ensure_device_id(self.state)
        identity.approve(self.identity(), state_dir=self.state, now=NOW,
                         terminal=_FakeTerminal([TAG, self.commit, "approve"]))
        identity.consume_consent(self.identity(), state_dir=self.state, now=NOW + 60)
        return self

    def __exit__(self, *args):
        self.install_patch.stop()
        self.tmp.cleanup()
        return False

    def _write_stage_receipt(self, **overrides):
        receipt = {"schema": update_staging.STAGE_SCHEMA,
                   "identity": self.identity(),
                   "identityDigest": self.digest(),
                   "manifestB64": base64.b64encode(self.manifest_bytes).decode("ascii"),
                   "sourceRoot": str(self.repo.resolve()),
                   "sourceCommit": self.commit,
                   "stage": str(self.stage_dir),
                   "git": "/usr/bin/git",
                   "createdAt": NOW + 120}
        receipt.update(overrides)
        self.stage_receipt.write_text(json.dumps(receipt, indent=1))
        os.chmod(str(self.stage_receipt), 0o600)
        return receipt

    def identity(self, **overrides):
        stored = {"repository": identity.REPOSITORY, "releaseId": 7, "tag": TAG,
                  "commit": self.commit, "assetId": 42,
                  "manifestDigest": hashlib.sha256(self.manifest_bytes).hexdigest(),
                  "publishedAt": STAMP, "assetUpdatedAt": STAMP}
        stored.update(overrides)
        return stored

    def digest(self, item=None):
        return identity.identity_digest(item if item is not None else self.identity())

    def begin_receipt(self, transaction_id=TXN, **overrides):
        receipt = dict(json.loads(self.stage_receipt.read_text()))
        receipt["transactionId"] = transaction_id
        receipt["drain"] = {"revision": OLD_COMMIT, "capability": CAPABILITY}
        receipt.update(overrides)
        return receipt


class _Case(unittest.TestCase):
    def setUp(self):
        self.fixture = _Fixture()
        self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        self.state, self.repo = self.fixture.state, self.fixture.repo

    def blocker(self, **kwargs):
        return update_transaction.admission_blocker(state_dir=self.state, **kwargs)


class TransactionJournalTest(_Case):
    """begin/transition: the private atomic journal and its phase compare-and-swap."""

    def test_begin_records_durable_prepared_transaction_with_barrier(self):
        record = update_transaction.begin_transaction(self.fixture.begin_receipt(),
                                                      state_dir=self.state)
        self.assertEqual(record["phase"], "prepared")
        stored = update_transaction.recover_transaction(state_dir=self.state)
        self.assertEqual(stored["transactionId"], TXN)
        self.assertEqual(stored["revision"], OLD_COMMIT, "the old exact commit is durable")
        self.assertEqual(stored["target"]["identity"]["tag"], TAG)
        self.assertEqual(stored["stage"]["path"], str(self.fixture.stage_dir))
        self.assertEqual(stored["inventory"]["sourceCommit"], self.fixture.commit)
        self.assertEqual(stored["drain"]["capability"], CAPABILITY,
                         "drain ownership is recoverable")
        self.assertEqual(stored["binding"], update_transaction.install_fingerprint(self.state))
        self.assertTrue(stored["state"], "external state is referenced by hash, never copied")
        barrier = update_transaction.read_barrier(state_dir=self.state)
        self.assertEqual(barrier["transactionId"], TXN)
        self.assertEqual(barrier["revision"], OLD_COMMIT)
        self.assertIsNotNone(self.blocker(), "a live transaction holds admission closed")

    def test_begin_refuses_a_second_live_transaction_and_an_incomplete_receipt(self):
        update_transaction.begin_transaction(self.fixture.begin_receipt(), state_dir=self.state)
        with self.assertRaises(identity.UpdateError) as caught:
            update_transaction.begin_transaction(self.fixture.begin_receipt(transaction_id="txn-2"),
                                                 state_dir=self.state)
        self.assertIn("one install holds one update transaction", str(caught.exception))
        other = _Fixture()
        other.__enter__()
        self.addCleanup(other.__exit__, None, None, None)
        receipt = other.begin_receipt()
        receipt.pop("sourceCommit")
        with self.assertRaises(identity.UpdateError):
            update_transaction.begin_transaction(receipt, state_dir=other.state)
        self.assertIsNone(update_transaction.recover_transaction(state_dir=other.state))

    def test_transition_is_a_compare_and_swap_that_refuses_replay_and_magic_phases(self):
        update_transaction.begin_transaction(self.fixture.begin_receipt(), state_dir=self.state)
        self.assertEqual(update_transaction.transition(TXN, "prepared", "draining",
                                                       state_dir=self.state,
                                                       note="drain re-proven")["phase"],
                         "draining")
        self.assertTrue(update_transaction.recover_transaction(state_dir=self.state)["evidence"])
        with self.assertRaises(identity.UpdateError):
            update_transaction.transition(TXN, "prepared", "draining", state_dir=self.state,
                                          note="a replay never rewrites history")
        # "activating" has had a consumer since #158; a phase beyond this
        # issue's lifecycle is still conjuring, not a state.
        with self.assertRaises(identity.UpdateError) as caught:
            update_transaction.transition(TXN, "draining", "finished", state_dir=self.state)
        self.assertIn("no consumer", str(caught.exception),
                      "a phase without a consumer in this issue is conjuring, not a state")
        with self.assertRaises(identity.UpdateError):
            update_transaction.transition("other-txn", "draining", "prepared",
                                          state_dir=self.state)
        self.assertEqual(update_transaction.recover_transaction(state_dir=self.state)["phase"],
                         "draining")

    def test_proven_abort_reopens_admission_and_clears_the_record(self):
        update_transaction.begin_transaction(self.fixture.begin_receipt(), state_dir=self.state)
        self.assertIsNotNone(self.blocker())
        update_transaction.transition(TXN, "prepared", "rolled_back", state_dir=self.state,
                                      reason="operator recover-abort")
        self.assertIsNone(update_transaction.read_barrier(state_dir=self.state),
                          "a finished transaction removes its barrier")
        self.assertIsNone(self.blocker())
        update_transaction.clear_transaction(TXN, state_dir=self.state)
        self.assertIsNone(update_transaction.recover_transaction(state_dir=self.state))

    def test_clear_refuses_a_live_transaction(self):
        update_transaction.begin_transaction(self.fixture.begin_receipt(), state_dir=self.state)
        with self.assertRaises(identity.UpdateError):
            update_transaction.clear_transaction(TXN, state_dir=self.state)
        self.assertIsNotNone(update_transaction.recover_transaction(state_dir=self.state))


class CrashDurabilityTest(_Case):
    """Every crash point leaves a complete record or nothing, never a torn one."""

    def test_crash_during_the_swap_leaves_no_partial_journal_or_barrier(self):
        with patch.object(os, "replace", side_effect=OSError(28, "no space")):
            with self.assertRaises(identity.UpdateError):
                update_transaction.begin_transaction(self.fixture.begin_receipt(),
                                                     state_dir=self.state)
        self.assertIsNone(update_transaction.recover_transaction(state_dir=self.state))
        self.assertIsNone(update_transaction.read_barrier(state_dir=self.state))
        self.assertIsNone(self.blocker(),
                          "a failed write reopens nothing and must not half-close")
        self.assertEqual([path.name for path in (self.state / "update").glob("*.tmp")],
                         [], "a failed write leaves no temporary record behind")

    def test_a_torn_record_is_refused_not_recovered(self):
        record = update_transaction.begin_transaction(self.fixture.begin_receipt(),
                                                      state_dir=self.state)
        journal = self.state / "update" / "transaction.json"
        journal.write_bytes(json.dumps(record, indent=1).encode("utf-8")[:80])
        with self.assertRaises(identity.UpdateError):
            update_transaction.recover_transaction(state_dir=self.state)
        refused = self.blocker()
        self.assertIsNotNone(refused, "a torn journal fails closed, never open")
        self.assertIn("transaction journal", refused["reason"])
        # a leftover temp file is data, never a record: readers ignore it
        journal.write_bytes(json.dumps(record, indent=1).encode("utf-8"))
        (self.state / "update" / "transaction.json.crash.tmp").write_text("garbage")
        self.assertIsNotNone(self.blocker(), "complete records and temps coexist safely")

    def test_a_foreign_record_for_another_install_is_refused(self):
        update_transaction.begin_transaction(self.fixture.begin_receipt(), state_dir=self.state)
        record = update_transaction.recover_transaction(state_dir=self.state)
        record["binding"] = {"device": "f" * 32, "root": "e" * 64}
        (self.state / "update" / "transaction.json").write_text(json.dumps(record))
        with self.assertRaises(identity.UpdateError) as caught:
            update_transaction.recover_transaction(state_dir=self.state)
        self.assertIn("different device or install", str(caught.exception))
        self.assertIsNotNone(self.blocker())
        self.assertEqual(self.blocker()["transactionId"], TXN)

    def test_a_concurrent_updater_process_is_refused(self):
        update_dir = self.state / "update"
        update_dir.mkdir(exist_ok=True)
        lock = update_dir / "transaction.lock"
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import os, sys, fcntl, time\n"
             "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
             "fcntl.flock(fd, fcntl.LOCK_EX)\n"
             "open(sys.argv[2], 'w').write('held')\n"
             "time.sleep(30)\n", str(lock), str(update_dir / "held")])
        try:
            while not (update_dir / "held").exists():
                if holder.poll() is not None:
                    raise AssertionError("lock holder died before the test could race it")
        finally:
            try:
                update_transaction.begin_transaction(self.fixture.begin_receipt(),
                                                     state_dir=self.state)
            except identity.UpdateError as caught:
                self.assertIn("another updater process", str(caught))
            else:
                self.fail("a concurrent process must not be able to begin a transaction")
            holder.kill()
            holder.wait()

    def test_the_records_are_private_and_the_dirs_are_0700(self):
        update_transaction.begin_transaction(self.fixture.begin_receipt(), state_dir=self.state)
        update = self.state / "update"
        for name in ("transaction.json", "admission-barrier.json", "transaction.lock"):
            info = (update / name).lstat()
            self.assertEqual(stat.S_IMODE(info.st_mode) & 0o77, 0, name + " stays private (0600)")
        self.assertEqual(stat.S_IMODE(update.stat().st_mode) & 0o77, 0)


class RelayBarrierTest(_Case):
    """The relay reads the private barrier at startup and refuses new admission."""

    def _relay(self):
        relay = Relay([AGENT], env={"AGENT_RELAY_STATE_DIR": str(self.state)})
        self.addCleanup(relay.close)
        return relay

    def test_restart_barrier_refuses_admission_then_a_proven_abort_reopens(self):
        update_transaction.begin_transaction(self.fixture.begin_receipt(), state_dir=self.state)
        relay = self._relay()
        status, body = relay.submit(agentId="worker", input="hello")
        self.assertEqual((status, body.get("error")), (503, "update_barrier_active"),
                         "the recorded transaction survives restart and closes admission")
        status, health = relay.request("GET", "/healthz")
        self.assertEqual(status, 200, "reads are not admissions")
        update_transaction.transition(TXN, "prepared", "rolled_back", state_dir=self.state,
                                      reason="operator recover-abort")
        update_transaction.clear_transaction(TXN, state_dir=self.state)
        relay.close()
        self.assertIsNone(update_transaction.read_barrier(state_dir=self.state))
        relay = self._relay()
        status, body = relay.submit(agentId="worker", input="hello")
        self.assertEqual(status, 202, "a fully proven abort gives back ordinary admission")

    def test_a_malformed_or_foreign_barrier_fails_closed(self):
        update_dir = self.state / "update"
        update_dir.mkdir(exist_ok=True)
        (update_dir / "admission-barrier.json").write_bytes(
            b'{"schema": "agent-relay.update-barrier/v1", "transactionId": "txn-')
        relay = self._relay()
        status, body = relay.submit(agentId="worker", input="hello")
        self.assertEqual((status, body.get("error")), (503, "update_barrier_active"),
                         "malformed update state fails closed, never silent")
        relay.close()

        foreign = {"schema": "agent-relay.update-barrier/v1", "transactionId": TXN,
                   "phase": "prepared", "revision": OLD_COMMIT,
                   "binding": {"device": "f" * 32, "root": "e" * 64}}
        (update_dir / "admission-barrier.json").write_text(json.dumps(foreign))
        relay = self._relay()
        status, body = relay.submit(agentId="worker", input="hello")
        self.assertEqual((status, body.get("error")), (503, "update_barrier_active"),
                         "a barrier for another device or install is not ours to ignore")
        self.assertEqual(relay.request("GET", "/healthz")[0], 200)

    def test_a_stale_barrier_outliving_its_record_still_fails_closed(self):
        update_transaction.begin_transaction(self.fixture.begin_receipt(), state_dir=self.state)
        # a crash after the journal end but before its barrier file was unlinked
        os.unlink(self.state / "update" / "transaction.json")
        self.assertIsNone(update_transaction.recover_transaction(state_dir=self.state))
        self.assertIsNotNone(self.blocker(), "a barrier with no record is stale and refuses")
        relay = self._relay()
        status, body = relay.submit(agentId="worker", input="hello")
        self.assertEqual((status, body.get("error")), (503, "update_barrier_active"))

    def test_a_malformed_live_journal_fails_closed_even_without_a_barrier(self):
        update_transaction.begin_transaction(self.fixture.begin_receipt(), state_dir=self.state)
        (self.state / "update" / "transaction.json").write_bytes(b"{trunc")
        (self.state / "update" / "admission-barrier.json").unlink()
        relay = self._relay()
        status, body = relay.submit(agentId="worker", input="hello")
        self.assertEqual((status, body.get("error")), (503, "update_barrier_active"))


    def test_an_incomplete_but_valid_json_journal_fails_closed(self):
        """Guard: a shaped-yet-incomplete journal record (valid JSON, missing
        the keys readers index) must fail closed into a 503 refusal, not a
        KeyError that kills the relay before it serves even reads."""
        update_dir = self.state / "update"
        update_dir.mkdir(mode=0o700, exist_ok=True)
        journal = update_dir / "transaction.json"
        journal.write_text(json.dumps({"schema": "agent-relay.update-transaction/v1"}))
        os.chmod(journal, 0o600)
        relay = self._relay()
        self.assertEqual(relay.request("GET", "/healthz")[0], 200,
                         "reads are not admissions and never die on incomplete state")
        status, body = relay.submit(agentId="worker", input="hello")
        self.assertEqual((status, body.get("error")), (503, "update_barrier_active"),
                         "an incomplete journal is untrustworthy and refuses admission")
        relay.close()

    def test_cli_recover_abort_reopens_admission_after_a_relay_restart(self):
        """Guard: after a restart the hold is gone (`drain_not_held`), yet the
        operator CLI — not a direct journal call — must still open admission."""
        probe = self._relay()
        revision = update_drain._server_revision(probe.base)
        probe.close()
        update_transaction.begin_transaction(
            self.fixture.begin_receipt(drain={"revision": revision,
                                              "capability": "c" * 64}),
            state_dir=self.state)
        relay = self._relay()
        status, body = relay.submit(agentId="worker", input="hello")
        self.assertEqual((status, body.get("error")), (503, "update_barrier_active"))
        relay.close()
        # The relaunched relay serves reads while its restarted barrier still refuses
        # admissions, and it is the relay the CLI must prove itself against.
        relay2 = self._relay()
        self.assertEqual(relay2.request("GET", "/healthz")[0], 200)
        with _join([patch.object(update_drain, "relay_token", lambda: TOKEN),
                    patch.object(safe_update, "_state_dir", lambda: self.state)]):
            self.assertEqual(safe_update.cmd_recover_abort(TXN, base_url=relay2.base), 0,
                             "a restarted relay's drain_not_held is accepted as proof "
                             "after the revision and unchanged-state proofs")
        status, body = relay2.submit(agentId="worker", input="hello")
        self.assertEqual((status, body.get("error")), (503, "update_barrier_active"),
                         "the relay that already read the barrier refuses until restart")
        relay2.close()
        relay3 = self._relay()
        status, body = relay3.submit(agentId="worker", input="hello")
        self.assertEqual(status, 202, "the CLI abort reopens ordinary admission")

    def test_a_broken_install_fingerprint_serves_reads_and_refuses_writes(self):
        """Guard: update state that cannot be bound to this device fails closed
        into a 503, never a relay process death."""
        update_transaction.begin_transaction(self.fixture.begin_receipt(), state_dir=self.state)
        (self.state / "device-id").unlink()
        relay = self._relay()
        self.assertEqual(relay.request("GET", "/healthz")[0], 200,
                         "reads are not admissions and never die on unreadable state")
        status, body = relay.submit(agentId="worker", input="hello")
        self.assertEqual((status, body.get("error")), (503, "update_barrier_active"))


class _join:
    def __init__(self, patches):
        self.patches = patches

    def __enter__(self):
        for item in self.patches:
            item.start()
        return self

    def __exit__(self, *args):
        for item in reversed(self.patches):
            item.stop()
        return False


class PrepareAbortCliTest(_Case):
    """`prepare-activation` / `recover-abort`: identity revalidated at the effect boundary."""

    def test_a_failed_prepare_after_the_acquire_releases_the_hold(self):
        """Guard: a begin_transaction failure must not strand the acquired drain hold."""
        relay = _FakeRelay()
        with _join(self._patches(relay) +
                   [patch.object(safe_update, "_state_dir", lambda: self.state),
                    patch.object(update_transaction, "begin_transaction",
                                 side_effect=identity.UpdateError(
                                     "transaction txn is still recorded"))]):
            with self.assertRaises(identity.UpdateError) as caught:
                safe_update.cmd_prepare_activation(TAG, str(self.repo),
                                                   base_url="http://127.0.0.1:9",
                                                   transaction_id=TXN)
        self.assertIn("released again", str(caught.exception),
                      "the error says the acquired hold was given back")
        self.assertEqual([call[0] for call in relay.calls], ["acquire", "release"],
                         "the hold acquired just before the failure is released again")
        self.assertEqual(relay.releases, [TXN])
        self.assertIsNone(update_transaction.recover_transaction(state_dir=self.state))
        self.assertIsNone(self.blocker())

    def test_a_failed_prepare_release_reports_an_unreleasable_hold(self):
        relay = _FakeRelay()

        def refusing_release(_base, transaction_id, *, token=None, revision=None,
                             capability=None, timeout=None):
            raise update_drain.DrainError("drain_error", "refused")
        relay.release = refusing_release
        with _join(self._patches(relay) +
                   [patch.object(safe_update, "_state_dir", lambda: self.state),
                    patch.object(update_transaction, "begin_transaction",
                                 side_effect=identity.UpdateError("no space"))]):
            with self.assertRaises(identity.UpdateError) as caught:
                safe_update.cmd_prepare_activation(TAG, str(self.repo),
                                                   base_url="http://127.0.0.1:9",
                                                   transaction_id=TXN)
        self.assertIn("could NOT be released", str(caught.exception),
                      "the error says the hold stands until an explicit release")

    def _patches(self, relay, release=None):
        """Fakes only the network hop, so the real ``_resolve`` shape is exercised."""
        release = release or (lambda tag, fetch_json: (self.fixture.identity(),
                                                       self.fixture.manifest,
                                                       self.fixture.manifest_bytes))
        receipt = dict(json.loads(self.fixture.stage_receipt.read_text()))
        return [
            patch.object(release_identity, "resolve_release", release),
            patch.object(safe_update, "_stage_receipt",
                         lambda tag, install_dir, state_dir: (receipt, self.fixture.identity())),
            patch.object(update_staging, "default_git", lambda: "/usr/bin/git"),
            patch.object(update_drain, "acquire_drain", relay.acquire),
            patch.object(update_drain, "drain_status", relay.status),
            patch.object(update_drain, "release_drain", relay.release),
            patch.object(update_drain, "_server_revision", relay.revision_of),
            patch.object(update_drain, "relay_token", lambda: TOKEN),
        ]

    def _prepared(self, relay, release=None):
        """Run prepare-activation via the CLI with the state dir patched."""
        patches = self._patches(relay, release) + [
            patch.object(safe_update, "_state_dir", lambda: self.state)]
        with _join(patches):
            return safe_update.main(["prepare-activation", TAG, "--install-dir", str(self.repo),
                                     "--base-url", "http://127.0.0.1:9",
                                     "--transaction-id", TXN])

    def _abort(self, relay, transaction=TXN, assert_raises=None):
        patches = self._patches(relay) + [
            patch.object(safe_update, "_state_dir", lambda: self.state)]
        with _join(patches):
            if assert_raises is None:
                return safe_update.main(["recover-abort", transaction,
                                         "--base-url", "http://127.0.0.1:9"])
            with self.assertRaises(assert_raises):
                safe_update.cmd_recover_abort(transaction, base_url="http://127.0.0.1:9")

    def test_prepare_holds_the_drain_writes_the_record_and_touches_nothing(self):
        relay = _FakeRelay()
        before_repo = _tree_inventory(self.repo)
        before_state = _tree_inventory(self.state)
        self.assertEqual(self._prepared(relay), 0)
        self.assertEqual([call[0] for call in relay.calls], ["acquire"],
                         "preparing only acquires the drain: no restart, no source activation, "
                         "no service control")
        self.assertEqual(relay.calls[0][1], TXN)
        self.assertEqual(relay.calls[0][2], TOKEN, "the token is sent, never stored")
        journal = update_transaction.recover_transaction(state_dir=self.state)
        raw_record = (self.state / "update" / "transaction.json").read_text()
        nonce = json.loads((self.state / "consent.json").read_text())["nonce"]
        self.assertNotIn(nonce, raw_record, "no consent secret is copied into the record")
        self.assertNotIn(base64.b64encode(self.fixture.manifest_bytes).decode("ascii")[:32],
                         raw_record, "no manifest content is copied into the record")
        self.assertEqual(_tree_inventory(self.repo), before_repo,
                         "preparing never changes the active source checkout")
        strip = lambda inv: {k: v for k, v in inv.items()
                             if (self.state / "update").as_posix() not in
                             (self.state / k).as_posix()}  # noqa: E731
        self.assertEqual(strip(_tree_inventory(self.state)), strip(before_state),
                         "consent.json and every other state file stay byte-identical")

    def test_publish_drift_between_staging_and_preparation_is_refused(self):
        relay = _FakeRelay()
        before = _tree_inventory(self.state)

        def drifted(tag, fetch_json=None):
            return (self.fixture.identity(commit="d" * 40), self.fixture.manifest,
                   self.fixture.manifest_bytes)
        with _join(self._patches(relay, release=drifted) +
                   [patch.object(safe_update, "_state_dir", lambda: self.state)]):
            with self.assertRaises(identity.UpdateError) as caught:
                safe_update.cmd_prepare_activation(TAG, str(self.repo),
                                                   base_url="http://127.0.0.1:9",
                                                   transaction_id=TXN)
        self.assertIn("mixed activation", str(caught.exception))
        self.assertEqual(relay.calls, [], "the refusal happens before any drain effect")
        self.assertEqual(_tree_inventory(self.state), before)
        self.assertIsNone(update_transaction.recover_transaction(state_dir=self.state))

    def test_mutated_stage_or_dirty_checkout_is_refused_before_the_effect(self):
        relay = _FakeRelay()
        self.fixture._write_stage_receipt(identityDigest="0" * 64)
        with _join(self._patches(relay) +
                   [patch.object(safe_update, "_state_dir", lambda: self.state)]):
            with self.assertRaises(identity.UpdateError) as caught:
                safe_update.cmd_prepare_activation(TAG, str(self.repo),
                                                   base_url="http://127.0.0.1:9",
                                                   transaction_id=TXN)
        self.assertIn("different identity", str(caught.exception))
        self.assertEqual(relay.calls, [], "the mutated stage never reaches the effect")
        self.fixture._write_stage_receipt()
        (self.repo / "dirty.txt").write_text("mutated\n")
        try:
            with _join(self._patches(relay) +
                       [patch.object(safe_update, "_state_dir", lambda: self.state)]):
                with self.assertRaises(update_staging.StagingError):
                    safe_update.cmd_prepare_activation(TAG, str(self.repo),
                                                       base_url="http://127.0.0.1:9",
                                                       transaction_id=TXN)
        finally:
            (self.repo / "dirty.txt").unlink()
            _run_git(self.repo, "add", "-A")
        self.assertEqual(relay.calls, [], "a dirty checkout never reaches the effect")
        self.assertIsNone(update_transaction.recover_transaction(state_dir=self.state))

    def test_abort_reopens_only_after_proving_revision_ownership_state_and_quiescence(self):
        relay = _FakeRelay()
        before_repo = _tree_inventory(self.repo)
        self.assertEqual(self._prepared(relay), 0)
        self.assertEqual(self._abort(relay), 0)
        self.assertEqual([call[0] for call in relay.calls],
                         ["acquire", "healthz", "status", "release"],
                         "abort proves the exact revision, the owned drain and its "
                         "quiescence, then releases as the last effect")
        self.assertEqual(relay.releases, [TXN])
        self.assertIsNone(update_transaction.recover_transaction(state_dir=self.state))
        self.assertIsNone(update_transaction.read_barrier(state_dir=self.state))
        self.assertIsNone(self.blocker())
        self.assertEqual(_tree_inventory(self.repo), before_repo,
                         "aborting never changes the active source")
        relay2 = _FakeRelay()
        self.assertEqual(self._prepared(relay2), 0,
                         "after a fully proven abort the install can prepare again")

    def test_abort_refuses_a_moved_revision_and_never_reopens(self):
        self._prepared(_FakeRelay())
        before = _tree_inventory(self.state / "update")
        relay = _FakeRelay(revision="e" * 40)
        self._abort(relay, assert_raises=identity.UpdateError)
        self.assertEqual(relay.calls, [("healthz",)], "nothing was proven, nothing released")
        self.assertEqual(_tree_inventory(self.state / "update"), before,
                         "a refused abort leaves the closed barrier intact")

    def test_abort_refuses_an_unknown_or_replayed_transaction_and_changed_state(self):
        self._prepared(_FakeRelay())
        relay = _FakeRelay()
        self._abort(relay, transaction="wrong-id", assert_raises=identity.UpdateError)
        self.assertEqual(relay.calls, [], "an unknown transaction aborts nothing")
        self.assertIsNotNone(self.blocker())

        # the hashed reference state changed after preparation
        consent = self.state / "consent.json"
        body = json.loads(consent.read_text())
        body["nonce"] = "f" * 32
        fd = os.open(str(consent), os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(body))
        relay = _FakeRelay()
        self._abort(relay, assert_raises=identity.UpdateError)
        self.assertEqual(relay.calls, [],
                         "changed state is proven before any hold is touched")
        self.assertIsNotNone(self.blocker(), "the barrier stays closed on a refused abort")

    def test_abort_refuses_a_relay_that_never_quiesced(self):
        self._prepared(_FakeRelay())
        relay = _FakeRelay()
        with _join(self._patches(relay) +
                   [patch.object(safe_update, "_state_dir", lambda: self.state),
                    patch.object(update_drain, "drain_status", lambda *a, **k: {
                        "quiesced": False, "active": 2, "queued": 1, "workflows": 0})]):
            with self.assertRaises(identity.UpdateError) as caught:
                safe_update.cmd_recover_abort(TXN, base_url="http://127.0.0.1:9")
        self.assertIn("manual recovery", str(caught.exception))
        self.assertEqual(relay.releases, [], "a still-running relay keeps its hold")


class StatusCliTest(_Case):
    def test_status_names_the_record_or_the_absence(self):
        update_transaction.begin_transaction(self.fixture.begin_receipt(), state_dir=self.state)
        with patch.object(safe_update, "_state_dir", lambda: self.state):
            self.assertEqual(safe_update.cmd_status(), 0)
        record = update_transaction.recover_transaction(state_dir=self.state)
        self.assertEqual(record["transactionId"], TXN)


if __name__ == "__main__":
    unittest.main()
