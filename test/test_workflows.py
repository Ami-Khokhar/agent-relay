"""Workflow engine: a step is a task, wired by needs, and its output feeds the next step."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_helpers import PYTHON, Relay

ECHO = "import sys; sys.stdout.write(sys.argv[1])"
EXIT_ON_FAIL = (
    "import sys\n"
    "print(sys.argv[1])\n"
    "sys.exit(1 if sys.argv[1].startswith('FAIL') else 0)\n"
)

TERMINAL = ("completed", "failed", "cancelled")


def echo(agent_id="echo"):
    return {"id": agent_id, "command": PYTHON, "args": ["-c", ECHO]}


def run(relay, spec):
    status, submitted = relay.request("POST", "/v1/workflows", spec)
    if status != 202:
        raise AssertionError(f"submit returned {status}: {submitted}")
    return wait(relay, submitted["id"])


def wait(relay, workflow_id, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, view = relay.request("GET", f"/v1/workflows/{workflow_id}")
        if view.get("status") in TERMINAL:
            return view
        time.sleep(0.02)
    raise RuntimeError("workflow did not finish")


def steps_of(workflow):
    return {step["id"]: step for step in workflow["steps"]}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="a2a-relay-wf-state-")
        self.relays = []

    def tearDown(self):
        for relay in self.relays:
            relay.close()
        shutil.rmtree(self.state, ignore_errors=True)

    def start(self, agents, env=None):
        relay = Relay(agents, env={"AGENT_RELAY_STATE_DIR": self.state, **(env or {})})
        self.relays.append(relay)
        return relay

    # ------------------------------------------------------------------ pipeline

    def test_runs_a_pipeline_and_threads_each_output_into_the_next_input(self):
        relay = self.start(echo())
        workflow = run(relay, {"name": "pipeline", "steps": [
            {"id": "draft", "agentId": "echo", "input": "alpha"},
            {"id": "polish", "agentId": "echo", "needs": ["draft"],
             "input": "polish({{draft.output}})"},
        ]})
        self.assertEqual(workflow["status"], "completed")
        self.assertRegex(workflow["id"], r"^wf-[0-9a-f]{32}$")
        finished = steps_of(workflow)
        self.assertEqual(finished["draft"]["output"], "alpha")
        self.assertEqual(finished["polish"]["output"], "polish(alpha)")
        self.assertEqual(finished["polish"]["attempts"], 1)

    def test_a_bare_step_id_renders_the_same_output(self):
        relay = self.start(echo())
        workflow = run(relay, {"steps": [
            {"id": "one", "agentId": "echo", "input": "x"},
            {"id": "two", "agentId": "echo", "needs": ["one"], "input": "[{{one}}]"},
        ]})
        self.assertEqual(steps_of(workflow)["two"]["output"], "[x]")

    def test_runs_independent_steps_concurrently_in_one_session(self):
        relay = self.start(echo())
        _, submitted = relay.request("POST", "/v1/workflows", {"steps": [
            {"id": "left", "agentId": "echo", "input": "L"},
            {"id": "right", "agentId": "echo", "input": "R"},
        ]})
        self.assertEqual(submitted["status"], "queued")
        workflow = wait(relay, submitted["id"])
        self.assertEqual(workflow["status"], "completed")
        _, tasks = relay.request("GET", f"/v1/tasks?sessionId={workflow['sessionId']}")
        self.assertEqual(len(tasks["tasks"]), 2)

    def test_skips_downstream_steps_when_a_dependency_fails(self):
        relay = self.start({"id": "gate", "command": PYTHON,
                            "args": ["-c", EXIT_ON_FAIL]})
        workflow = run(relay, {"steps": [
            {"id": "boom", "agentId": "gate", "input": "FAIL this"},
            {"id": "after", "agentId": "gate", "needs": ["boom"], "input": "later"},
        ]})
        self.assertEqual(workflow["status"], "failed")
        self.assertEqual(workflow["error"], "step boom failed")
        finished = steps_of(workflow)
        self.assertEqual(finished["boom"]["status"], "failed")
        self.assertEqual(finished["after"]["status"], "skipped")
        self.assertEqual(finished["after"]["error"], "dependency_not_completed")
        self.assertEqual(finished["after"]["taskIds"], [])

    def test_a_template_reference_outside_needs_fails_the_step_when_it_starts(self):
        relay = self.start(echo())
        workflow = run(relay, {"steps": [
            {"id": "a", "agentId": "echo", "input": "x"},
            {"id": "b", "agentId": "echo", "input": "{{a.output}}"},
        ]})
        self.assertEqual(workflow["status"], "failed")
        self.assertIn("unresolved template reference", steps_of(workflow)["b"]["error"])

    # ---------------------------------------------------------------- submission

    def test_returns_the_same_run_for_a_reused_request_id(self):
        relay = self.start(echo())
        spec = {"name": "idem", "requestId": "run-1",
                "steps": [{"id": "only", "agentId": "echo", "input": "once"}]}
        status, first = relay.request("POST", "/v1/workflows", spec)
        self.assertEqual(status, 202)
        status, again = relay.request("POST", "/v1/workflows", spec)
        self.assertEqual(status, 200)
        self.assertEqual(again["id"], first["id"])
        status, body = relay.request("POST", "/v1/workflows", dict(spec, name="different"))
        self.assertEqual((status, body["error"]), (409, "idempotency_conflict"))

    def test_reports_an_unknown_run(self):
        relay = self.start(echo())
        status, body = relay.request("GET", "/v1/workflows/wf-deadbeef000000000000000000000000")
        self.assertEqual((status, body["error"]), (404, "unknown_workflow"))

    def test_evicts_the_oldest_finished_run_and_releases_its_request_id(self):
        relay = self.start(echo(), env={"AGENT_RELAY_MAX_WORKFLOWS": "1"})
        first = run(relay, {"requestId": "keep-1",
                            "steps": [{"id": "a", "agentId": "echo", "input": "one"}]})
        second = run(relay, {"steps": [{"id": "a", "agentId": "echo", "input": "two"}]})
        status, body = relay.request("GET", f"/v1/workflows/{first['id']}")
        self.assertEqual((status, body["error"]), (404, "unknown_workflow"))
        self.assertEqual(relay.request("GET", f"/v1/workflows/{second['id']}")[0], 200)
        # The evicted run's requestId is free again, so a retry starts new work.
        status, again = relay.request("POST", "/v1/workflows", {
            "requestId": "keep-1", "steps": [{"id": "a", "agentId": "echo", "input": "three"}]})
        self.assertEqual(status, 202)
        self.assertNotEqual(again["id"], first["id"])

    def test_advertises_the_step_limit_on_healthz(self):
        relay = self.start(echo())
        _, health = relay.request("GET", "/healthz")
        self.assertEqual(health["limits"]["maxSteps"], 64)

    def test_refuses_more_steps_than_the_limit(self):
        relay = self.start(echo(), env={"AGENT_RELAY_MAX_STEPS": "2"})
        self.assertEqual(relay.request("GET", "/healthz")[1]["limits"]["maxSteps"], 2)
        status, body = relay.request("POST", "/v1/workflows", {"steps": [
            {"id": "a", "agentId": "echo", "input": "1"},
            {"id": "b", "agentId": "echo", "input": "2"},
            {"id": "c", "agentId": "echo", "input": "3"}]})
        self.assertEqual((status, body["error"]), (400, "invalid_steps"))

    def test_rejects_a_step_whose_cwd_is_outside_the_allowed_roots(self):
        relay = self.start({"id": "rooted", "command": PYTHON, "args": ["-c", ECHO],
                            "allowedRoots": [self.state]})
        status, body = relay.request("POST", "/v1/workflows", {"steps": [
            {"id": "a", "agentId": "rooted", "input": "x",
             "cwd": tempfile.gettempdir() + "/.."},
        ]})
        self.assertEqual((status, body["error"]), (400, "cwd_not_allowed"))

    # ------------------------------------------------------------------ validation

    def test_rejects_a_malformed_document_with_a_specific_error(self):
        base = {"id": "a", "agentId": "echo", "input": "x"}
        cases = [
            ("empty_steps", {"steps": []}, "invalid_steps"),
            ("unknown_workflow_field", {"bogus": 1, "steps": [base]}, "unknown_field"),
            ("unknown_step_field", {"steps": [{**base, "nope": 1}]}, "unknown_field"),
            ("missing_input", {"steps": [{"id": "a", "agentId": "echo"}]}, "input_required"),
            ("duplicate_step", {"steps": [base, dict(base)]}, "duplicate_step"),
            ("unknown_agent", {"steps": [{"id": "a", "agentId": "ghost", "input": "x"}]},
             "unknown_agent"),
            ("unknown_need", {"steps": [{**base, "needs": ["zzz"]}]}, "unknown_step"),
            ("self_need", {"steps": [{**base, "needs": ["a"]}]}, "cyclic_dependency"),
            ("cycle", {"steps": [{**base, "needs": ["b"]},
                                 {"id": "b", "agentId": "echo", "input": "y", "needs": ["a"]}]},
             "cyclic_dependency"),
            ("malformed_placeholder", {"steps": [{**base, "input": "{{a.output"}]},
             "invalid_template"),
            ("zero_timeout", {"steps": [{**base, "timeoutMs": 0}]}, "invalid_timeout"),
            ("bad_cwd", {"steps": [{**base, "cwd": ""}]}, "invalid_cwd"),
        ]
        relay = self.start(echo())
        for name, spec, expected in cases:
            with self.subTest(name):
                status, body = relay.request("POST", "/v1/workflows", spec)
                self.assertEqual(status, 400, body)
                self.assertEqual(body["error"], expected)


if __name__ == "__main__":
    unittest.main()
