import os
import hashlib
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from team_execution import TeamExecutor
from team_wake import TeamWake
from teams import TeamError, TeamStore


class TeamWakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, "teams.db")
        self.store = TeamStore(self.path)
        self.run_id = self.store.create_run("goal", "lead", [
            {"id": "worker", "agentId": "configured-worker"}],
            {"max_concurrency": 2})["id"]
        self.store.set_phase(self.run_id, "lead", "specification", "criteria", approved=True)
        self.store.set_phase(self.run_id, "lead", "build")
        self.assignment_id = self.store.assign(self.run_id, "lead", "worker", "implement")
        self.children = []

    def executor(self, store=None):
        store = store or self.store

        def create_task(body):
            task_id = "task-{}".format(len(self.children) + 1)
            self.children.append(body)
            return 202, {"id": task_id, "status": "queued"}

        return TeamExecutor(store, create_task, lambda task_id: {"id": task_id, "status": "queued"})

    def record_prior_dispatch(self, source=True):
        after_seq = max((event["seq"] for event in self.store.get_run(self.run_id)["events"]), default=0)
        executor = self.executor()
        attempt = executor.dispatch(self.run_id, self.assignment_id, "initial")
        self.store.record_attempt_result(self.run_id, attempt["id"], "completed", {"ok": True})
        self.store.record_dispatch_provenance(
            self.run_id, self.assignment_id, 1,
            os.path.abspath(self.temp.name) if source else None,
            "a" * 40 if source else None, after_seq)
        return attempt

    def message(self, body="New detail"):
        return self.store.post_message(self.run_id, "lead", "worker", body, kind="question")

    def test_wake_is_durable_idempotent_and_ack_does_not_trigger_work(self):
        # This unread message was already included in the first prompt.
        old_event = self.message("Initial instructions")
        self.record_prior_dispatch()
        self.assertEqual(TeamStore(self.path).get_dispatch_provenance(
            self.run_id, self.assignment_id, 1)["base_commit"], "a" * 40)
        store = TeamStore(self.path)
        executor = self.executor(store)
        wake = TeamWake(store, lambda run, assignment, request, provenance:
                        executor.dispatch(run, assignment, request))
        self.assertEqual(wake.wake(self.run_id, "worker"), [])
        new_event = self.message("Please review this change")
        first = wake.wake(self.run_id, "worker")
        self.assertEqual(first[0]["status"], "dispatched")
        self.assertEqual(len(self.children), 2)
        self.assertNotEqual(first[0]["event_id"], old_event)
        replay = wake.wake(self.run_id, "worker")
        self.assertEqual(replay[0]["reason"], "already_processed")
        self.assertEqual(replay[0]["event_id"], new_event)
        self.assertEqual(len(self.children), 2)
        store.acknowledge_delivery(self.run_id, "worker", new_event)
        self.assertEqual(wake.wake(self.run_id, "worker"), [])
        self.assertEqual(len(self.children), 2)

    def test_wake_waits_until_existing_assignment_attempt_finishes(self):
        prior = self.executor().dispatch(self.run_id, self.assignment_id, "initial")
        self.store.record_dispatch_provenance(self.run_id, self.assignment_id, 1)
        event = self.message()
        wake = TeamWake(self.store, lambda run, assignment, request, provenance:
                        self.executor().dispatch(run, assignment, request))
        waiting = wake.wake(self.run_id, "worker")
        self.assertEqual(waiting[0]["reason"], "assignment_active")
        self.assertEqual(len(self.children), 1)
        self.store.record_attempt_result(self.run_id, prior["id"], "completed", {})
        self.assertEqual(wake.wake(self.run_id, "worker")[0]["event_id"], event)
        self.assertEqual(len(self.children), 2)

    def test_message_during_initial_submit_is_after_snapshot_and_wakes(self):
        after_seq = max((event["seq"] for event in self.store.get_run(self.run_id)["events"]), default=0)

        def create_task(body):
            self.children.append(body)
            self.message("Arrived while the first task was submitting")
            return 202, {"id": "initial-task", "status": "queued"}

        executor = TeamExecutor(self.store, create_task,
                                lambda task_id: {"id": task_id, "status": "queued"})
        initial = executor.dispatch(self.run_id, self.assignment_id, "initial")
        self.store.record_attempt_result(self.run_id, initial["id"], "completed", {})
        self.store.record_dispatch_provenance(self.run_id, self.assignment_id, 1, after_seq=after_seq)
        wake = TeamWake(self.store, lambda run, assignment, request, provenance:
                        self.executor().dispatch(run, assignment, request))
        self.assertEqual(wake.wake(self.run_id, "worker")[0]["status"], "dispatched")
        self.assertEqual(len(self.children), 2)

    def test_prepare_attempt_serializes_competing_revision_dispatches(self):
        barrier = threading.Barrier(2)
        successes, errors = [], []

        def prepare(request_id):
            barrier.wait()
            try:
                successes.append(self.store.prepare_attempt(
                    self.run_id, self.assignment_id, request_id, "prompt", 1,
                    expected_spec_hash=hashlib.sha256(b"criteria").hexdigest()))
            except TeamError as exc:
                errors.append(exc.code)

        threads = [threading.Thread(target=prepare, args=(f"race-{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(successes), 1)
        self.assertEqual(errors, ["assignment_active"])

    def test_closed_or_expired_runs_do_not_wake(self):
        self.record_prior_dispatch()
        self.message()
        wake = TeamWake(self.store, lambda *args: self.fail("closed run dispatched"))
        self.store.close_run(self.run_id, "lead")
        self.assertEqual(wake.wake(self.run_id)[0]["reason"], "run_closed")

    def test_elapsed_run_limit_stops_wake(self):
        self.record_prior_dispatch()
        self.message()
        with self.store._db() as db:
            db.execute("UPDATE runs SET created=? WHERE id=?", (time.time() - 4000, self.run_id))
        wake = TeamWake(self.store, lambda *args: self.fail("expired run dispatched"))
        self.assertEqual(wake.wake(self.run_id)[0]["reason"], "time_limit")


if __name__ == "__main__":
    unittest.main()
