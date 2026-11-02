"""Failed activation recovers to the exact, verified prior revision (#159).

The recovery consumer — the automatic rollback a failed ``activate`` attempt
runs, and the explicit ``safe_update recover`` command — restores the
transaction's exact journaled prior revision, never a branch or a tag, and
never an intervening user edit; it restarts the positively validated per-user
service only once per recovery, only when the running relay does not already
report the restored revision or the restore moved the tree the running relay
was built from, and it holds the durable admission barrier until an
authenticated endpoint reports that exact prior revision. Every failed proof
keeps the ``recovery_required`` record, its barrier and an actionable remedy,
and never declares the rollback complete. Boundaries are faked at the service
manager, the HTTP endpoints and the participating git objects, as in the
activation tests; no user service, no publication and no host state is
touched.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import release_identity as identity
import safe_update
import update_drain
import update_service
import update_staging
import update_transaction
from test_update_activation import _Fixture, _Case, _run_git, CAPABILITY, TXN


class _RollbackCase(_Case):
    """The activation fixture plus a configurable relay probe and release."""

    def setUp(self):
        super().setUp()
        self.serving = self.fixture.old   # the commit the fake relay was started with
        self.serving_stuck = False        # set True: the relay never picks up new code
        self.refresh_raises = None
        self.probe_raises = None
        self._fault_patchers = []
        self._patch(safe_update, "_authenticated_get", self._fake_auth_get)
        self._patch(safe_update, "_refresh_barrier", self._refresh)
        self._patch(update_drain, "release_drain", self._fake_release)

    def _fake_release(self, _base, transaction_id, *, token=None, revision=None,
                      capability=None, **_):
        self.calls.append(("release", transaction_id))
        if revision != self.fixture.old or capability != CAPABILITY:
            raise update_drain.DrainError("revision_mismatch", "refused")
        return {"transactionId": transaction_id}

    def _refresh(self, _base, _token):
        if self.refresh_raises is not None:
            raise self.refresh_raises
        return {"admissionClosed": False}

    def _fake_auth_get(self, _base, _token, path):
        self.calls.append(("get", path))
        if path == safe_update.BARRIER_PATH:
            record = update_transaction.read_journal(state_dir=self.state)
            if record is not None and record["phase"] not in ("activated", "rolled_back"):
                return {"admissionClosed": True,
                        "barrier": {"transactionId": TXN, "reason": "activation live"}}
            return {"admissionClosed": False, "barrier": None}
        if self.probe_raises is not None:
            raise self.probe_raises
        return {"agents": [], "revision": {"commit": self.serving}}

    def fake_restart(self, service):
        self.calls.append(("restart", service["kind"], service["path"]))
        # a restart puts the relay on the source that is on disk right now;
        # a stuck relay (serving_stuck) keeps its original provenance
        if not self.serving_stuck:
            self.serving = self._head()
        argv = ["/usr/bin/fake-manager", "--user", "restart", service["label"]]
        self.restart_commands.append(argv)
        return {"kind": service["kind"], "commands": [argv], "restartedAt": 1.0}

    def failing_restart(self, service):
        self.calls.append(("restart-failed", service["kind"]))
        raise identity.UpdateError("the user launchd refused the restart")

    def run_recover(self, **kwargs):
        self.fixture.write_plist()
        kw = {"deadline_s": 1.0, "restart": self.fake_restart}
        kw.update(kwargs)
        return safe_update.cmd_recover(TXN, **kw)

    def _crash(self, *, swapped):
        """Replay an activation to its boundary and leave it durably failed.

        The journal is walked to ``activating`` (and, with ``swapped``, through
        the real source swap to ``restarted``) and then advanced to the
        ``recovery_required`` end _record_recovery writes for a real
        interruption, so every recovery test starts from a realistic durable
        mid-activation state.
        """
        record = update_transaction.recover_transaction(state_dir=self.state)
        update_transaction.transition(TXN, "prepared", "draining",
                                      state_dir=self.state, quiesced=True)
        update_transaction.transition(TXN, "draining", "quiescent",
                                      state_dir=self.state, service={"kind": "launchd"})
        update_transaction.transition(TXN, "quiescent", "activating", state_dir=self.state,
                                      oldRevision=self.fixture.old,
                                      targetCommit=self.fixture.target)
        if swapped:
            update_staging.activate_transaction_source(record, git=self._git())
            update_transaction.transition(TXN, "activating", "restarted",
                                          state_dir=self.state, commit=self.fixture.target)
        update_transaction.transition(
            TXN, update_transaction.read_journal(state_dir=self.state)["phase"],
            "recovery_required", state_dir=self.state, reason="simulated interruption")

    def _git(self):
        return update_staging.Git("/usr/bin/git")

    def _head(self):
        return _run_git("-C", str(self.repo), "rev-parse", "HEAD")

    def assertConverged(self, restarts=None, restarts_note=None):
        self.assertIsNone(update_transaction.read_journal(state_dir=self.state))
        self.assertIsNone(update_transaction.read_barrier(state_dir=self.state))
        self.assertIsNone(update_transaction.admission_blocker(state_dir=self.state))
        self.assertEqual(self._head(), self.fixture.old,
                         "the checkout runs the exact journaled prior revision")
        self.assertEqual(self.serving, self.fixture.old,
                         "the fake relay runs the restored revision")
        if restarts is not None:
            self.assertEqual(len(self.restart_commands), restarts)


class AutomaticRollbackTest(_RollbackCase):
    """A failed activate attempt recovers itself to the exact prior revision."""

    def run_activate(self, *, restart=None, deadline=0.4):
        self.fixture.write_plist()
        return safe_update.cmd_activate(
            TXN, str(self.repo), inspect=self.fake_inspect,
            restart=restart if restart is not None else self.fake_restart,
            deadline_s=deadline)

    def test_a_health_verdict_that_never_matches_is_rolled_back_automatically(self):
        """The relay keeps reporting the old commit, so the activation can never
        verify its target and the automatic rollback restores the exact prior
        tree, restarts the same inspected service boundary once and finishes
        the transaction without a repeated action."""
        self.serving_stuck = True
        old_tree = _run_git("-C", str(self.repo), "cat-file", "-p", "HEAD")
        rc = self.run_activate()
        self.assertEqual(rc, 1)
        self.assertConverged(restarts=1)
        self.assertEqual(_run_git("-C", str(self.repo), "cat-file", "-p", "HEAD"),
                         old_tree, "the exact prior tree was restored")
        self.assertEqual(self.calls[-1][0], "release",
                         "the finish releases the hold, then re-opens the relay's barrier")

    def test_a_health_check_that_fails_keeps_the_whole_failed_state(self):
        self.health_broken = True

        def refusing(_commit, *, authenticated_request=None, deadline_s=None):
            if self.health_broken:
                raise identity.UpdateError("the health endpoint refused the relay")
            return {"revision": {"commit": _commit}}
        self._patch(update_service, "await_revision", refusing)
        self.probe_raises = update_drain.DrainError("drain_unreachable", "down")
        with self.assertRaises(identity.UpdateError):
            self.run_activate()
        # the rollback restarted once but could never verify the old revision
        # through the broken endpoint, so the failed state and its barrier stay
        record = update_transaction.recover_transaction(state_dir=self.state)
        self.assertEqual(record["phase"], "recovery_required")
        self.assertIsNotNone(update_transaction.read_barrier(state_dir=self.state))
        self.assertIsNotNone(update_transaction.admission_blocker(state_dir=self.state))
        self.assertEqual(self._head(), self.fixture.old,
                         "the restore itself succeeded before the health proof failed")
        self.assertEqual(len(self.restart_commands), 2,
                         "one restart for the attempt and one for the rollback")
        # once the endpoint answers again, recovery proves and finalizes without
        # repeating the service action
        self.health_broken = False
        self.probe_raises = None
        with self.assertRaises(identity.UpdateError):
            self.run_activate()
        record = update_transaction.recover_transaction(state_dir=self.state)
        self.assertEqual(record["phase"], "recovery_required")
        self.probe_raises = None
        self.run_recover()
        self.assertConverged(restarts=2)

    def test_a_failing_service_start_is_finished_by_the_explicit_command(self):
        # the restart fails for the activation and for its automatic rollback,
        # so the failed state stands with its barrier until the operator's recover
        self.probe_raises = update_drain.DrainError("drain_unreachable", "down")
        with self.assertRaises(identity.UpdateError):
            self.run_activate(restart=self.failing_restart)
        record = update_transaction.recover_transaction(state_dir=self.state)
        self.assertEqual(record["phase"], "recovery_required")
        self.assertIsNotNone(update_transaction.read_barrier(state_dir=self.state))
        self.assertEqual(self._head(), self.fixture.old)
        self.probe_raises = None
        self.run_recover()
        self.assertConverged(restarts=0)


class RecoveryCommandTest(_RollbackCase):
    """``safe_update recover`` rolls the durable failed activation back."""

    def test_crash_after_the_swap_is_recovered_to_the_exact_prior_revision(self):
        self._crash(swapped=True)
        self.assertEqual(self._head(), self.fixture.target)


    def test_crash_before_the_swap_needs_no_restart_at_all(self):
        self._crash(swapped=False)
        self.run_recover()
        self.assertConverged(restarts=0)
        self.assertEqual(self.restart_commands, [],
                         "a relay that already serves the prior revision is not restarted")

    def test_repeated_recovery_after_success_never_repeats_the_service_action(self):
        self._crash(swapped=True)
        self.run_recover()
        first = len(self.restart_commands)
        with self.assertRaises(identity.UpdateError) as caught:
            self.run_recover()
        self.assertIn("nothing to recover", str(caught.exception))
        self.assertEqual(len(self.restart_commands), first,
                         "the finished record is not recovered again")
        self.assertConverged()

    def test_a_terminal_rolled_back_record_finalizes_read_only(self):
        """A crash between the durable finalize and the expunge leaves a
        ``rolled_back`` record that only owes one barrier refresh; recovery
        retries exactly that and repeats no service action."""
        self._crash(swapped=False)
        record = update_transaction.read_journal(state_dir=self.state)
        record["phase"] = "rolled_back"
        update_transaction._write_journal(self.state, record)
        self.assertIsNone(update_transaction.read_barrier(state_dir=self.state))
        self.refresh_raises = update_drain.DrainError("refresh_refused", "unreachable")
        with self.assertRaises(identity.UpdateError) as caught:
            self.run_recover()
        self.assertIn("reopened admission", str(caught.exception))
        self.assertEqual(self.restart_commands, [])
        self.refresh_raises = None
        self.run_recover()
        self.assertConverged(restarts=0)

    def test_another_transaction_id_is_refused_as_a_replay(self):
        self._crash(swapped=False)
        with self.assertRaises(identity.UpdateError) as caught:
            safe_update.cmd_recover("other-transaction", deadline_s=1.0,
                                    restart=self.fake_restart)
        self.assertIn("refusing to replay", str(caught.exception))
        self.assertEqual(update_transaction.read_journal(state_dir=self.state)["phase"],
                         "recovery_required")
        self.assertEqual(self.restart_commands, [])

    def test_a_wrong_device_journal_is_refused_and_kept(self):
        self._crash(swapped=False)
        path = self.state / "update" / "transaction.json"
        record = json.loads(path.read_text())
        record["binding"]["device"] = "f" * 32
        path.write_text(json.dumps(record))
        os.chmod(str(path), 0o600)
        with self.assertRaises(identity.UpdateError) as caught:
            self.run_recover()
        self.assertIn("different device or install", str(caught.exception))
        self.assertIsNotNone(update_transaction.read_barrier(state_dir=self.state))
        self.assertEqual(self.restart_commands, [])

    def test_intervening_user_edits_are_never_overwritten(self):
        self._crash(swapped=True)
        readme = self.repo / "README.md"
        readme.write_text("# my own uncommitted work\n")
        with self.assertRaises(identity.UpdateError) as caught:
            self.run_recover()
        self.assertIn("uncommitted or untracked", str(caught.exception))
        record = update_transaction.recover_transaction(state_dir=self.state)
        self.assertEqual(record["phase"], "recovery_required")
        self.assertIsNotNone(update_transaction.read_barrier(state_dir=self.state))
        self.assertEqual(self._head(), self.fixture.target)
        self.assertEqual(readme.read_text(), "# my own uncommitted work\n",
                         "the user's edit is intact")
        self.assertEqual(self.restart_commands, [], "no service action for a refused tree")
        readme.write_text("# agent-relay new\n")
        self.run_recover()
        self.assertConverged(restarts=0)


class RecoveryBoundaryTest(_RollbackCase):
    """Every journal/effect boundary is refused durably and recovered safely."""

    def _inject(self, boundary):
        """Fault one boundary; the fault lasts until the next _reset call."""
        if boundary == "restart":
            # a relay restarted onto the target source before the crash: the
            # recovery restart is then required and faulted by the pending kw
            self.serving = self.fixture.target
        self._fault(boundary)

    def _reset_faults(self):
        for patcher in self._fault_patchers:
            patcher.stop()
        self._fault_patchers = []
        self.refresh_raises = None

    def _patched_kw(self, boundary, pending):
        """One-shot per-call fault overrides; the pending set is consumed once."""
        kw = {}
        if boundary in pending:
            pending.remove(boundary)
            kw["restart"] = self.failing_restart
        return kw

    def _fault(self, boundary):
        if boundary == "recovery-note":
            def broken(*_a, **_k):
                raise identity.UpdateError("the recovery journal could not be written")
            self._fault_patchers.append(patch.object(
                update_transaction, "journal_recovery_note", broken))
            self._fault_patchers[-1].start()
        elif boundary == "restore":
            def broken(*_a, **_k):
                raise update_staging.StagingError("the restore was interrupted")
            self._fault_patchers.append(patch.object(
                update_staging, "rollback_transaction_source", broken))
            self._fault_patchers[-1].start()
        elif boundary == "rolled_back":
            original = update_transaction.transition_locked

            def refused(transaction_id, expected_phase, next_phase, **kwargs):
                if next_phase == "rolled_back":
                    raise identity.UpdateError("the finalize journal could not be written")
                return original(transaction_id, expected_phase, next_phase, **kwargs)
            self._fault_patchers.append(patch.object(
                update_transaction, "transition_locked", refused))
            self._fault_patchers[-1].start()
        elif boundary == "release":
            def refused(*_a, **_k):
                raise update_drain.DrainError("drain_refused_409", "the relay refused")
            self._fault_patchers.append(patch.object(update_drain, "release_drain", refused))
            self._fault_patchers[-1].start()
        elif boundary == "refresh":
            self.refresh_raises = update_drain.DrainError("refresh_refused", "unreachable")

    def test_every_journal_and_effect_boundary_refuses_then_recovers(self):
        for boundary in ("recovery-note", "restore", "restart", "rolled_back",
                         "release", "refresh"):
            with self.subTest(boundary=boundary):
                pending = {boundary}
                self._reset_faults()
                self._swap_fixture()
                self._inject(boundary)
                self._crash(swapped=True)
                head_before = self._head()   # target: the swap had happened
                try:
                    self.run_recover(**self._patched_kw(boundary, pending))
                except identity.UpdateError as exc:
                    self.last_message = str(exc)
                else:
                    self.fail("recovery did not fail at the %s boundary" % boundary)
                self._assert_boundary_state(boundary, head_before)
                # once the fault clears, recovery converges
                self._reset_faults()
                self.run_recover(**self._patched_kw(boundary, pending))
                self.assertConverged()

    def _assert_boundary_state(self, boundary, head_before):
        record = update_transaction.recover_transaction(state_dir=self.state)
        if boundary in ("release", "refresh"):
            self.assertEqual(record["phase"], "rolled_back")
            self.assertIsNone(update_transaction.read_barrier(state_dir=self.state))
            self.assertEqual(self._head(), self.fixture.old)
        else:
            self.assertEqual(record["phase"], "recovery_required")
            self.assertIsNotNone(
                update_transaction.read_barrier(state_dir=self.state),
                "the barrier never reopens on a failed boundary")
            # a fault at restart-or-later leaves the restored tree
            self.assertEqual(self._head(), head_before if boundary in
                             ("recovery-note", "restore") else self.fixture.old)
            self.assertIsNotNone(
                update_transaction.admission_blocker(state_dir=self.state))
            self.assertIn("recover", self.last_message, "the refusal names the remedy")

    def _swap_fixture(self):
        """A fresh fixture for one subTest boundary, the HOME patch re-bound."""
        self.home_patch.stop()
        self.fixture.__exit__(None, None, None)
        self.fixture = _Fixture()
        self.fixture.__enter__()
        self.state, self.repo = self.fixture.state, self.fixture.repo
        self.calls, self.restart_commands = [], []
        self.serving = self.fixture.old
        # the stopped HOME patch reverted the env patch's keys too, so the
        # relay environment is re-applied before HOME is bound to the new home
        self.env_patch.stop()
        self.env_patch = patch.dict(os.environ, {
            "AGENT_RELAY_URL": "http://127.0.0.1-fake", "AGENT_RELAY_TOKEN": "t" * 32})
        self.env_patch.start()
        self.home_patch = patch.dict(os.environ, {"HOME": str(self.fixture.home)})
        self.home_patch.start()


class PrivateStateTest(_RollbackCase):
    """Private config and service definitions are retained unchanged (#159)."""

    def _inventory(self):
        got = {}
        for name, rel in (("consent", "consent.json"), ("device", "device-id")):
            path = self.state / rel
            data = path.read_bytes()
            got[name] = "%s:%o" % (hashlib.sha256(data).hexdigest(),
                                   stat.S_IMODE(path.stat().st_mode))
        for name, rel in (("barrier", "update/admission-barrier.json"),
                          ("journal", "update/transaction.json")):
            path = self.state / rel
            if not path.exists():
                continue
            data = path.read_bytes()
            got[name] = "%s:%o" % (hashlib.sha256(data).hexdigest(),
                                   stat.S_IMODE(path.stat().st_mode))
        receipt = Path(self.fixture.stage) / "stage-receipt.json"
        got["receipt"] = "%s:%o" % (hashlib.sha256(receipt.read_bytes()).hexdigest(),
                                    stat.S_IMODE(receipt.stat().st_mode))
        self.fixture.write_plist()
        plist = self.fixture.home / "Library" / "LaunchAgents" / "com.agent-relay.plist"
        got["service"] = "%s:%o" % (hashlib.sha256(plist.read_bytes()).hexdigest(),
                                    stat.S_IMODE(plist.stat().st_mode))
        got["updateDir"] = "%o" % stat.S_IMODE((self.state / "update").stat().st_mode)
        return got

    def test_state_stays_private_and_unchanged_through_recovery(self):
        self._crash(swapped=True)
        before = self._inventory()
        self.assertEqual(before["barrier"].split(":")[1], "600")
        self.assertEqual(before["journal"].split(":")[1], "600")
        self.assertEqual(before["updateDir"], "700")
        live = dict(before)
        live.pop("barrier")
        live.pop("journal")
        self.run_recover()
        after = self._inventory()
        self.assertEqual(after, live, "consent, device id, stage receipt and the "
                         "service definition are byte- and permission-unchanged")
        self.assertFalse((self.state / "update" / "transaction.json").exists(),
                         "the finished record is expunged, not rewritten")

    def test_the_journal_carries_references_and_digests_never_secret_payloads(self):
        """Guard: the old recovery_required record also kept state private and
        referenced external state by digest only."""
        self._crash(swapped=True)
        inventory = self._inventory()
        self.assertEqual(inventory["barrier"].split(":")[1], "600")
        self.assertEqual(inventory["journal"].split(":")[1], "600")
        journal = (self.state / "update" / "transaction.json").read_bytes().decode()
        self.assertNotIn("t" * 8, journal, "the relay token is never journaled")
        self.assertNotIn("a" * 32, journal, "the consent nonce is never journaled")
        for point in json.loads(journal)["state"]:
            self.assertRegex(point["digest"], r"^[0-9a-f]{64}$",
                             "external references are digests, not contents")


if __name__ == "__main__":
    unittest.main()
