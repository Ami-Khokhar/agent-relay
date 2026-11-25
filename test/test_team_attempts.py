import os
import hashlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from teams import TeamError, TeamStore


class AttemptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, "teams.db")
        self.store = TeamStore(self.path)
        self.run_id = self.store.create_run("goal", "lead", [{"id": "a"}, {"id": "b"}], {"max_concurrency": 1})["id"]
        self.store.set_phase(self.run_id, "lead", "specification", "acceptance", approved=True)
        self.store.set_phase(self.run_id, "lead", "build")
        self.aid = self.store.assign(self.run_id, "lead", "a", "implement")

    def prepare(self, request="req", revision=1):
        return self.store.prepare_attempt(self.run_id, self.aid, request, "prompt", revision)

    def test_prepare_is_idempotent_and_counts_preparing_toward_atomic_limit(self):
        first = self.prepare()
        again = self.prepare()
        self.assertTrue(first["newly_created"])
        self.assertFalse(again["newly_created"])
        self.assertEqual(first["id"], again["id"])
        second_assignment = self.store.assign(self.run_id, "lead", "b", "another")
        with self.assertRaises(TeamError) as err:
            self.store.prepare_attempt(self.run_id, second_assignment, "other", "prompt", 1)
        self.assertEqual(err.exception.code, "concurrency_limit")

    def test_request_id_conflict_and_stale_assignment_revision_are_rejected(self):
        self.prepare()
        with self.assertRaises(TeamError) as err:
            self.store.prepare_attempt(self.run_id, self.aid, "req", "different", 1)
        self.assertEqual(err.exception.code, "request_id_conflict")
        self.store.assign(self.run_id, "lead", "a", "revised")
        with self.assertRaises(TeamError) as err:
            self.store.prepare_attempt(self.run_id, self.aid, "new", "prompt", 1)
        self.assertEqual(err.exception.code, "stale_assignment")

    def test_preparation_rejects_spec_changed_since_prompt_was_built(self):
        expected = hashlib.sha256(b"acceptance").hexdigest()
        self.store.set_phase(self.run_id, "lead", "specification", "changed and reapproved", approved=True)
        self.store.set_phase(self.run_id, "lead", "build")
        with self.assertRaises(TeamError) as err:
            self.store.prepare_attempt(self.run_id, self.aid, "stale-spec", "prompt based on old spec", 1,
                                       expected_spec_hash=expected)
        self.assertEqual((err.exception.status, err.exception.code), (409, "stale_spec"))

    def test_queued_running_transition_and_terminal_result_are_immutable(self):
        attempt = self.prepare()
        queued = self.store.record_attempt_submission(self.run_id, attempt["id"], "task-1", "queued")
        running = self.store.record_attempt_submission(self.run_id, attempt["id"], "task-1", "running")
        self.assertEqual((queued["status"], running["status"]), ("queued", "running"))
        done = self.store.record_attempt_result(self.run_id, attempt["id"], "completed", {"ok": True})
        self.assertEqual(done["result"], {"ok": True})
        with self.assertRaises(TeamError) as err:
            self.store.record_attempt_result(self.run_id, attempt["id"], "failed", {})
        self.assertEqual(err.exception.code, "attempt_terminal")

    def test_timed_out_is_persisted_as_terminal_status(self):
        attempt = self.prepare()
        result = self.store.record_attempt_result(self.run_id, attempt["id"], "timed_out", {"seconds": 30})
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(self.store.list_attempts(self.run_id)[0]["status"], "timed_out")

    def test_running_old_revision_blocks_dispatch_on_reused_assignment(self):
        attempt = self.prepare()
        self.store.record_attempt_submission(self.run_id, attempt["id"], "task-old", "running")
        self.store.assign(self.run_id, "lead", "a", "revised assignment")
        with self.assertRaises(TeamError) as err:
            self.store.prepare_attempt(self.run_id, self.aid, "new-revision", "new prompt", 2)
        self.assertEqual(err.exception.code, "assignment_active")
        self.store.record_attempt_result(self.run_id, attempt["id"], "completed", {})
        self.assertEqual(self.store.prepare_attempt(
            self.run_id, self.aid, "new-revision", "new prompt", 2)["status"], "preparing")

    def test_dependency_requires_completion_of_pinned_revision(self):
        dependency = self.store.assign(self.run_id, "lead", "b", "prepare input")
        self.store.assign(self.run_id, "lead", "a", "implement", [dependency])
        with self.assertRaises(TeamError) as err:
            self.prepare("blocked", revision=2)
        self.assertEqual(err.exception.code, "dependency_not_ready")
        dep_attempt = self.store.prepare_attempt(self.run_id, dependency, "dep", "prompt", 1)
        self.store.record_attempt_result(self.run_id, dep_attempt["id"], "completed", {"artifact": "input"})
        self.assertEqual(self.prepare("ready", revision=2)["status"], "preparing")

    def test_submission_status_and_ids_are_validated(self):
        attempt = self.prepare()
        for method in (
            lambda: self.store.record_attempt_submission(self.run_id, attempt["id"], "", "queued"),
            lambda: self.store.prepare_attempt(" ", self.aid, "x", "prompt", 1),
        ):
            with self.assertRaises(TeamError) as err:
                method()
            self.assertEqual(err.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
