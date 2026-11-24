"""Team execution tests use the durable store and fake only the task boundary."""
import os
import json
import hashlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from team_execution import TeamExecutionError, TeamExecutor
from teams import TeamStore


class TaskBoundary:
    def __init__(self):
        self.tasks = {}
        self.requests = []

    def create(self, request):
        self.requests.append(request)
        task_id = f"task-{len(self.requests)}"
        task = {"id": task_id, "status": "queued", "agentId": request["agentId"],
                "input": request["input"], "requestId": request["requestId"]}
        self.tasks[task_id] = task
        return 202, task

    def lookup(self, task_id):
        return self.tasks.get(task_id)

    def cancel(self, task):
        if task and task["status"] in ("queued", "running"):
            task["status"] = "cancelled"
        return task


class TeamExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = TeamStore(os.path.join(self.temp.name, "teams.db"))
        self.boundary = TaskBoundary()
        self.clock = [1000.0]
        self.executor = TeamExecutor(self.store, self.boundary.create, self.boundary.lookup,
                                     self.boundary.cancel, clock=lambda: self.clock[0])
        self.run_id = self.store.create_run("Improve the product", "lead", [
            {"id": "worker", "agentId": "configured-worker", "role": "worker",
             "harness": "claude", "model": "model-a"},
            {"id": "reviewer", "agentId": "configured-reviewer", "role": "reviewer",
             "harness": "codex", "model": "model-b"},
        ], {"max_concurrency": 2, "max_elapsed_seconds": 90})["id"]
        self.created = self.store.get_run(self.run_id)["created"]
        self.clock[0] = self.created + 1
        self.store.set_phase(self.run_id, "lead", "specification", "Acceptance: behavior is correct", approved=True)
        self.store.set_phase(self.run_id, "lead", "build")
        self.first = self.store.assign(self.run_id, "lead", "worker", "Implement the accepted change")

    def test_dispatch_persists_before_submission_and_reuses_request_id(self):
        result = self.executor.dispatch(self.run_id, self.first, "request-1")
        self.assertEqual(result["status"], "queued")
        self.assertEqual(len(self.boundary.requests), 1)
        self.assertEqual(self.boundary.requests[0]["agentId"], "configured-worker")
        expected = "team:" + hashlib.sha256(f"{self.run_id}\0request-1".encode()).hexdigest()
        self.assertEqual(self.boundary.requests[0]["requestId"], expected)
        self.assertIn("Role: Implement only the assigned work", self.boundary.requests[0]["input"])
        retry = self.executor.dispatch(self.run_id, self.first, "request-1")
        self.assertEqual(retry["id"], result["id"])
        self.assertEqual(len(self.boundary.requests), 1)

    def test_idempotent_retry_does_not_rebuild_prompt_from_new_events(self):
        result = self.executor.dispatch(self.run_id, self.first, "request-1")
        original_prompt = self.boundary.requests[0]["input"]
        self.store.post_message(self.run_id, "lead", "worker", "A later note", assignment_id=self.first)
        retry = self.executor.dispatch(self.run_id, self.first, "request-1")
        self.assertEqual(retry["id"], result["id"])
        self.assertEqual(len(self.boundary.requests), 1)
        self.assertEqual(self.boundary.requests[0]["input"], original_prompt)

    def test_unknown_submission_is_not_replayed_until_startup_recovery(self):
        def uncertain(_request):
            raise TimeoutError("response lost")
        self.executor.create_task = uncertain
        with self.assertRaises(TeamExecutionError) as err:
            self.executor.dispatch(self.run_id, self.first, "request-unknown")
        self.assertEqual(err.exception.code, "submission_unknown")
        retry = self.executor.dispatch(self.run_id, self.first, "request-unknown")
        self.assertEqual(retry["status"], "submission_unknown")
        self.assertEqual(len(self.boundary.requests), 0)
        recovered = self.executor.recover(self.run_id)[0]
        self.assertEqual(recovered["status"], "interrupted")

    def test_dependency_requires_latest_revision_to_complete(self):
        dependent = self.store.assign(self.run_id, "lead", "reviewer", "Review implementation",
                                      dependencies=[self.first], purpose="implementation")
        with self.assertRaises(TeamExecutionError) as err:
            self.executor.dispatch(self.run_id, dependent, "review-1")
        self.assertEqual(err.exception.code, "dependency_not_ready")
        attempt = self.executor.dispatch(self.run_id, self.first, "request-1")
        self.boundary.tasks[attempt["task_id"]].update(status="completed", output="Implemented")
        self.executor.refresh(self.run_id, attempt["id"])
        self.store.assign(self.run_id, "lead", "worker", "Revise implementation")
        with self.assertRaises(TeamExecutionError) as err:
            self.executor.dispatch(self.run_id, dependent, "review-2")
        self.assertEqual(err.exception.code, "dependency_not_ready")

    def test_closed_run_and_elapsed_limit_refuse_dispatch(self):
        self.store.close_run(self.run_id, "lead", "Done")
        with self.assertRaises(TeamExecutionError) as err:
            self.executor.dispatch(self.run_id, self.first, "request-closed")
        self.assertEqual(err.exception.code, "run_closed")
        self.assertEqual(self.boundary.requests, [])

    def test_recovery_marks_missing_task_interrupted_without_replay(self):
        attempt = self.executor.dispatch(self.run_id, self.first, "request-1")
        del self.boundary.tasks[attempt["task_id"]]
        recovered = self.executor.recover(self.run_id)[0]
        self.assertEqual(recovered["status"], "interrupted")
        result = recovered["result"]
        if isinstance(result, str):
            result = json.loads(result)
        self.assertFalse(result["replacement_context"]["native_session_resumed"])
        self.assertIn("request-1", recovered["request_id"])
        self.assertEqual(len(self.boundary.requests), 1)

    def test_startup_recovery_interrupts_preexisting_preparing_attempt(self):
        run = self.store.get_run(self.run_id)
        assignment = run["assignments"][0]
        self.store.prepare_attempt(self.run_id, self.first, "crashed-before-submit", "saved prompt",
                                   assignment_revision=assignment["revision"], purpose="implementation")
        recovered = self.executor.recover(self.run_id)[0]
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(self.boundary.requests, [])

    def test_timed_out_child_status_is_preserved(self):
        attempt = self.executor.dispatch(self.run_id, self.first, "request-timeout")
        self.boundary.tasks[attempt["task_id"]].update(status="timed_out", error="time limit")
        timed_out = self.executor.refresh(self.run_id, attempt["id"])
        self.assertEqual(timed_out["status"], "timed_out")

    def test_spec_change_between_prompt_and_atomic_prepare_is_rejected(self):
        prepare = self.store.prepare_attempt

        def change_spec_then_prepare(*args, **kwargs):
            self.store.set_phase(self.run_id, "lead", "specification", "Revised acceptance criteria", approved=True)
            self.store.set_phase(self.run_id, "lead", "build")
            return prepare(*args, **kwargs)

        self.store.prepare_attempt = change_spec_then_prepare
        with self.assertRaises(TeamExecutionError) as err:
            self.executor.dispatch(self.run_id, self.first, "request-stale-spec")
        self.assertEqual(err.exception.code, "stale_spec")
        self.assertEqual(self.boundary.requests, [])

    def test_large_history_is_bounded_for_recovery_and_replacement_dispatch(self):
        for index in range(35):
            self.store.post_message(self.run_id, "lead", "worker", f"Message {index}: " + "m" * 3000,
                                    assignment_id=self.first)
        for index in range(3):
            prior = self.executor.dispatch(self.run_id, self.first, f"history-{index}")
            self.boundary.tasks[prior["task_id"]].update(status="completed", output=f"Result {index}: " + "r" * 12000)
            self.executor.refresh(self.run_id, prior["id"])
        lost = self.executor.dispatch(self.run_id, self.first, "history-lost")
        self.boundary.tasks.pop(lost["task_id"])
        interrupted = self.executor.recover(self.run_id)[-1]
        result = interrupted["result"]
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode("utf-8")), 32768)
        context = result["replacement_context"]
        self.assertGreater(context["omitted_older_events"], 0)
        self.assertIn("Improve the product", context["objective"])
        replacement = self.executor.dispatch(self.run_id, self.first, "history-replacement")
        prompt = self.boundary.requests[-1]["input"]
        self.assertEqual(replacement["status"], "queued")
        self.assertLessEqual(len(prompt.encode("utf-8")), 256 * 1024)
        self.assertIn("older events omitted:", prompt)
        self.assertIn("retrieve the complete agreed", prompt)
        self.assertIn("AGENT_RELAY_TEAM_ID", prompt)
        self.assertIn('"native_session_resumed": false', prompt)

    def test_completed_task_eviction_does_not_rewrite_terminal_attempt(self):
        attempt = self.executor.dispatch(self.run_id, self.first, "request-1")
        self.boundary.tasks[attempt["task_id"]].update(status="completed", output="Done")
        terminal = self.executor.refresh(self.run_id, attempt["id"])
        self.boundary.tasks.clear()
        again = self.executor.refresh(self.run_id, attempt["id"])
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(again["status"], "completed")

    def test_large_task_output_is_durably_truncated_with_marker(self):
        attempt = self.executor.dispatch(self.run_id, self.first, "request-large")
        self.boundary.tasks[attempt["task_id"]].update(status="completed", output="x" * 50000)
        completed = self.executor.refresh(self.run_id, attempt["id"])
        result = completed["result"]
        self.assertEqual(completed["status"], "completed")
        self.assertTrue(result["recordTruncated"])
        self.assertTrue(result["outputTruncated"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode("utf-8")), 32768)

    def test_replacement_prompt_carries_prior_result_without_claiming_resume(self):
        attempt = self.executor.dispatch(self.run_id, self.first, "request-1")
        self.boundary.tasks[attempt["task_id"]].update(status="completed", output="Implemented with evidence")
        self.executor.refresh(self.run_id, attempt["id"])
        second = self.executor.dispatch(self.run_id, self.first, "request-2")
        prompt = self.boundary.requests[-1]["input"]
        self.assertEqual(second["status"], "queued")
        self.assertIn("Implemented with evidence", prompt)
        self.assertIn("Acceptance: behavior is correct", prompt)
        self.assertIn('"native_session_resumed": false', prompt)

    def test_research_can_dispatch_before_spec_approval_but_implementation_cannot(self):
        self.store.set_phase(self.run_id, "lead", "exploration", approved=False)
        research = self.store.assign(self.run_id, "lead", "worker", "Inspect the existing behavior", purpose="research")
        result = self.executor.dispatch(self.run_id, research, "research-1")
        self.assertEqual(result["status"], "queued")
        implementation = self.store.assign(self.run_id, "lead", "reviewer", "Implement the change")
        self.store.set_phase(self.run_id, "lead", "specification", "A changed acceptance spec", approved=False)
        with self.assertRaises(TeamExecutionError) as err:
            self.executor.dispatch(self.run_id, implementation, "implementation-1")
        self.assertEqual(err.exception.code, "phase_gate")


if __name__ == "__main__":
    unittest.main()
