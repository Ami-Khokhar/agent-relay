"""Workflow engine tests: validation, scheduling, fan-out, retries, loops, cancel, resume."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_helpers import PRINT_ARGV, PYTHON, Relay

ECHO = "import sys; sys.stdout.write(sys.argv[1])"
# Fails the first time it runs, then succeeds: the mark file is the memory.
FLAKY = (
    "import os, sys, pathlib\n"
    "mark = pathlib.Path(os.environ['WF_MARK'])\n"
    "if mark.exists():\n"
    "    print('recovered')\n"
    "else:\n"
    "    mark.write_text('1')\n"
    "    sys.stderr.write('transient failure')\n"
    "    sys.exit(1)\n"
)
# Reports a finding on the first call and none afterwards, so `repeat` can watch it.
COUNTING = (
    "import os, sys, pathlib\n"
    "counter = pathlib.Path(os.environ['WF_COUNT'])\n"
    "rounds = int(counter.read_text()) if counter.exists() else 0\n"
    "counter.write_text(str(rounds + 1))\n"
    "print('findings: %d' % (1 if rounds == 0 else 0))\n"
)
EXIT_ON_FAIL = (
    "import sys\n"
    "print(sys.argv[1])\n"
    "sys.exit(1 if sys.argv[1].startswith('FAIL') else 0)\n"
)
# A CLI-shaped failure: banner lines first, the real cause last, on a line long enough that
# keeping the whole transcript would flood the caller.
BANNER_THEN_ERROR = (
    "import sys\n"
    "for line in ('Reading additional input from stdin...', 'compiling excuses',\n"
    "             'still working'):\n"
    "    sys.stderr.write(line + '\\n')\n"
    "sys.stderr.write(\"ERROR: usage limit; try again at 4:40 PM \" + 'x' * 8000 + '\\n')\n"
    "sys.exit(1)\n"
)
SLOW = "import time; time.sleep(30)"

TERMINAL = ("completed", "failed", "cancelled")


def echo(agent_id="echo"):
    return {"id": agent_id, "command": PYTHON, "args": ["-c", ECHO]}


def optioned():
    """An agent that echoes its argv, so a test can see which option args were added."""
    return {"id": "optioned", "command": PYTHON, "args": [*PRINT_ARGV],
            "options": {"effort": {"low": ["--low"], "high": ["--high"]}}}


def run(relay, spec):
    """Submit a workflow and poll it to a terminal state."""
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
        self.data = tempfile.mkdtemp(prefix="a2a-relay-wf-data-")
        self.relays = []

    def tearDown(self):
        for relay in self.relays:
            relay.close()
        shutil.rmtree(self.state, ignore_errors=True)
        shutil.rmtree(self.data, ignore_errors=True)

    def start(self, agents, env=None):
        relay = Relay(agents, env={"AGENT_RELAY_STATE_DIR": self.state, **(env or {})})
        self.relays.append(relay)
        return relay

    def stop(self, relay):
        relay.close()
        self.relays.remove(relay)

    def path(self, name):
        return os.path.join(self.data, name)

    # ------------------------------------------------------------------ pipeline

    def test_runs_a_linear_pipeline_and_threads_each_output_into_the_next_input(self):
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

    def test_runs_independent_steps_concurrently_and_shares_one_session_id(self):
        relay = self.start(echo())
        status, submitted = relay.request("POST", "/v1/workflows", {"steps": [
            {"id": "left", "agentId": "echo", "input": "L"},
            {"id": "right", "agentId": "echo", "input": "R"},
        ]})
        self.assertEqual(status, 202)
        workflow = wait(relay, submitted["id"])
        self.assertEqual(workflow["status"], "completed")
        session_id = workflow["sessionId"]
        _, tasks = relay.request("GET", f"/v1/tasks?sessionId={session_id}")
        self.assertEqual(len(tasks["tasks"]), 2)
        self.assertEqual({task["sessionId"] for task in tasks["tasks"]}, {session_id})

    def test_long_poll_returns_once_the_run_reaches_a_terminal_state(self):
        relay = self.start(echo())
        _, submitted = relay.request("POST", "/v1/workflows", {"steps": [
            {"id": "only", "agentId": "echo", "input": "fast"},
        ]})
        status, view = relay.request(
            "GET", f"/v1/workflows/{submitted['id']}?waitMs=8000")
        self.assertEqual(status, 200)
        self.assertIn(view["status"], TERMINAL)
        self.assertEqual(view["spec"]["steps"][0]["input"], "fast")

    # -------------------------------------------------------------------- fan-out

    def test_fans_out_with_foreach_and_passes_the_array_to_the_next_step(self):
        relay = self.start(echo())
        workflow = run(relay, {"steps": [
            {"id": "fan", "agentId": "echo", "foreach": ["one", "two"],
             "input": "item={{item}} index={{index}}"},
            {"id": "collect", "agentId": "echo", "needs": ["fan"],
             "input": "collected={{fan.output}}"},
        ]})
        self.assertEqual(workflow["status"], "completed")
        finished = steps_of(workflow)
        self.assertEqual(finished["fan"]["output"],
                         json.dumps(["item=one index=0", "item=two index=1"]))
        self.assertEqual(len(finished["fan"]["taskIds"]), 2)
        self.assertEqual(finished["collect"]["output"],
                         'collected=["item=one index=0", "item=two index=1"]')

    # --------------------------------------------------------- failure and retry

    def test_skips_downstream_steps_when_a_dependency_fails(self):
        relay = self.start({"id": "gate", "command": PYTHON,
                            "args": ["-c", EXIT_ON_FAIL]})
        workflow = run(relay, {"steps": [
            {"id": "boom", "agentId": "gate", "input": "FAIL this"},
            {"id": "after", "agentId": "gate", "needs": ["boom"], "input": "later"},
        ]})
        self.assertEqual(workflow["status"], "failed")
        # The run's error carries the failed step's short cause: the last lines of the
        # agent's stderr ("Agent exited with 1") and output ("FAIL this").
        self.assertEqual(workflow["error"], "step boom failed: Agent exited with 1\nFAIL this")
        finished = steps_of(workflow)
        self.assertEqual(finished["boom"]["status"], "failed")
        self.assertEqual(finished["after"]["status"], "skipped")
        self.assertEqual(finished["after"]["error"], "dependency_not_completed")
        self.assertEqual(finished["after"]["taskIds"], [])

    def test_retries_a_failed_step_once_before_giving_up(self):
        mark = self.path("retry-mark")
        relay = self.start({"id": "flaky", "command": PYTHON, "args": ["-c", FLAKY],
                            "env": {"WF_MARK": mark}})
        workflow = run(relay, {"steps": [
            {"id": "once", "agentId": "flaky", "input": "go", "retries": 1},
        ]})
        self.assertEqual(workflow["status"], "completed")
        finished = steps_of(workflow)
        self.assertEqual(finished["once"]["attempts"], 2)
        self.assertEqual(finished["once"]["output"], "recovered")

    def test_fails_a_step_when_every_retry_is_spent(self):
        relay = self.start({"id": "gate", "command": PYTHON,
                            "args": ["-c", EXIT_ON_FAIL]})
        workflow = run(relay, {"steps": [
            {"id": "doomed", "agentId": "gate", "input": "FAIL always", "retries": 2},
        ]})
        self.assertEqual(workflow["status"], "failed")
        finished = steps_of(workflow)
        self.assertEqual(finished["doomed"]["status"], "failed")
        self.assertEqual(finished["doomed"]["attempts"], 3)

    def test_a_failed_step_shows_the_last_lines_the_agent_printed(self):
        relay = self.start({"id": "noisy", "command": PYTHON, "args": ["-c", BANNER_THEN_ERROR]})
        workflow = run(relay, {"steps": [
            {"id": "codex", "agentId": "noisy", "input": "go"},
        ]})
        self.assertEqual(workflow["status"], "failed")
        error = steps_of(workflow)["codex"]["error"]
        self.assertIn("usage limit", error)
        # The banner is the first line the agent printed; the cause is the last, and the
        # long tail is capped so the summary stays short.
        self.assertNotIn("Reading additional input from stdin", error)
        self.assertLess(len(error), 3000)
        # The run's own error carries the same cause, not just the step id.
        self.assertIn("usage limit", workflow["error"])

    def test_a_quota_failure_is_named_agent_quota_with_its_retry_time(self):
        relay = self.start({"id": "noisy", "command": PYTHON, "args": ["-c", BANNER_THEN_ERROR]})
        workflow = run(relay, {"steps": [
            {"id": "codex", "agentId": "noisy", "input": "go"},
        ]})
        error = steps_of(workflow)["codex"]["error"]
        self.assertTrue(error.startswith("agent_quota: "), error)
        self.assertIn("try again at 4:40 PM", error)

    def test_the_full_stderr_stays_available_as_error_detail(self):
        relay = self.start({"id": "noisy", "command": PYTHON, "args": ["-c", BANNER_THEN_ERROR]})
        _, submitted = relay.request("POST", "/v1/workflows", {"steps": [
            {"id": "codex", "agentId": "noisy", "input": "go"},
        ]})
        workflow = wait(relay, submitted["id"])
        step = steps_of(workflow)["codex"]
        self.assertIn("usage limit", step["error"])
        # The single-run view keeps the whole transcript behind its own field.
        self.assertIn("Reading additional input from stdin", step["errorDetail"])
        self.assertIn("try again at 4:40 PM", step["errorDetail"])
        self.assertGreater(len(step["errorDetail"]), len(step["error"]))
        # The list summary carries the short cause only.
        _, listing = relay.request("GET", "/v1/workflows")
        listed = listing["workflows"][0]["steps"][0]
        self.assertNotIn("errorDetail", listed)
        self.assertNotIn("output", listed)

    # ---------------------------------------------------------------- step timing

    def test_gives_each_step_and_each_attempt_its_own_time_window(self):
        mark = self.path("times-mark")
        relay = self.start([
            echo(),
            {"id": "flaky", "command": PYTHON, "args": ["-c", FLAKY],
             "env": {"WF_MARK": mark}},
        ])
        status, submitted = relay.request("POST", "/v1/workflows", {"name": "times", "steps": [
            {"id": "one", "agentId": "echo", "input": "alpha"},
            {"id": "two", "agentId": "flaky", "needs": ["one"], "input": "beta",
             "retries": 1},
        ]})
        self.assertEqual(status, 202)
        # Nothing has run yet, so every window is still empty.
        for step in submitted["steps"]:
            self.assertIn("startedAt", step)
            self.assertIn("finishedAt", step)
            self.assertIsNone(step["startedAt"])
            self.assertIsNone(step["finishedAt"])

        workflow = wait(relay, submitted["id"])
        self.assertEqual(workflow["status"], "completed")
        first, second = steps_of(workflow)["one"], steps_of(workflow)["two"]
        for step in (first, second):
            self.assertIsNotNone(step["startedAt"])
            self.assertIsNotNone(step["finishedAt"])
            self.assertLessEqual(step["startedAt"], step["finishedAt"])
        self.assertLessEqual(first["startedAt"], second["startedAt"])
        self.assertLessEqual(first["finishedAt"], second["startedAt"])

        self.assertEqual(second["attempts"], 2)
        windows = second["attemptTimes"]
        self.assertEqual(len(windows), second["attempts"])
        for window in windows:
            self.assertIsNotNone(window["startedAt"])
            self.assertIsNotNone(window["finishedAt"])
            self.assertLessEqual(window["startedAt"], window["finishedAt"])
        self.assertLessEqual(windows[0]["startedAt"], windows[1]["startedAt"])
        self.assertLessEqual(windows[0]["finishedAt"], windows[1]["startedAt"])

    # ---------------------------------------------------------- per-step task options

    def test_passes_step_options_through_to_the_child_task(self):
        relay = self.start(optioned())
        workflow = run(relay, {"steps": [
            {"id": "only", "agentId": "optioned", "input": "go", "options": {"effort": "high"}},
        ]})
        self.assertEqual(workflow["status"], "completed")
        self.assertEqual(steps_of(workflow)["only"]["output"], json.dumps(["--high", "go"]))

    def test_fails_a_step_whose_agent_cannot_run_read_only(self):
        relay = self.start(echo())
        workflow = run(relay, {"steps": [
            {"id": "only", "agentId": "echo", "input": "go", "readOnly": True},
        ]})
        self.assertEqual(workflow["status"], "failed")
        error = steps_of(workflow)["only"]["error"]
        self.assertTrue(error.startswith("read_only_unsupported: "), error)
        self.assertIn("no readOnly profile", error)

    # --------------------------------------------------------------------- loops

    def test_repeats_a_step_until_its_output_matches(self):
        relay = self.start({"id": "review", "command": PYTHON, "args": ["-c", COUNTING],
                            "env": {"WF_COUNT": self.path("rounds-pass")}})
        workflow = run(relay, {"steps": [
            {"id": "check", "agentId": "review", "input": "review",
             "repeat": {"max": 3, "untilOutputMatches": r"findings: 0"}},
        ]})
        self.assertEqual(workflow["status"], "completed")
        finished = steps_of(workflow)
        self.assertEqual(finished["check"]["attempts"], 2)
        self.assertEqual(finished["check"]["output"], "findings: 0")

    def test_fails_a_repeating_step_when_the_round_limit_is_reached(self):
        relay = self.start({"id": "review", "command": PYTHON, "args": ["-c", COUNTING],
                            "env": {"WF_COUNT": self.path("rounds-fail")}})
        workflow = run(relay, {"steps": [
            {"id": "check", "agentId": "review", "input": "review",
             "repeat": {"max": 1, "untilOutputMatches": r"findings: 0"}},
        ]})
        self.assertEqual(workflow["status"], "failed")
        finished = steps_of(workflow)
        self.assertEqual(finished["check"]["status"], "failed")
        self.assertEqual(finished["check"]["error"], "repeat_limit_reached")
        self.assertEqual(finished["check"]["attempts"], 1)

    # ------------------------------------------------------------------- control

    def test_cancel_stops_the_run_and_its_running_children(self):
        relay = self.start({"id": "slow", "command": PYTHON, "args": ["-c", SLOW]})
        _, submitted = relay.request("POST", "/v1/workflows", {"steps": [
            {"id": "wait", "agentId": "slow", "input": "sleep"},
        ]})
        workflow_id = submitted["id"]
        deadline = time.time() + 10
        view = {}
        while time.time() < deadline:
            _, view = relay.request("GET", f"/v1/workflows/{workflow_id}")
            if view["steps"][0]["status"] == "running":
                break
            time.sleep(0.02)
        self.assertEqual(view["steps"][0]["status"], "running")

        status, cancelled = relay.request("DELETE", f"/v1/workflows/{workflow_id}")
        self.assertEqual(status, 200)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["steps"][0]["status"], "cancelled")

        _, tasks = relay.request("GET", "/v1/tasks")
        self.assertEqual(len(tasks["tasks"]), 1)
        self.assertEqual(tasks["tasks"][0]["status"], "cancelled")

        # A second cancel is a no-op rather than an error.
        status, again = relay.request("DELETE", f"/v1/workflows/{workflow_id}")
        self.assertEqual(status, 200)
        self.assertEqual(again["status"], "cancelled")

    def test_resume_skips_the_steps_that_already_completed(self):
        mark = self.path("resume-mark")
        agents = [
            echo(),
            {"id": "flaky", "command": PYTHON, "args": ["-c", FLAKY],
             "env": {"WF_MARK": mark}},
        ]
        relay = self.start(agents)
        _, submitted = relay.request("POST", "/v1/workflows", {"name": "resume", "steps": [
            {"id": "first", "agentId": "echo", "input": "kept"},
            {"id": "second", "agentId": "flaky", "needs": ["first"], "input": "retry me"},
        ]})
        workflow_id = submitted["id"]
        failed = wait(relay, workflow_id)
        self.assertEqual(failed["status"], "failed")
        kept = steps_of(failed)["first"]
        self.assertEqual(kept["output"], "kept")
        # The step that the resume will skip must already carry its window, so the journal
        # has something to keep.
        self.assertIsNotNone(kept["startedAt"])
        self.assertIsNotNone(kept["finishedAt"])
        self.assertLessEqual(kept["startedAt"], kept["finishedAt"])
        self.assertEqual(len(kept["attemptTimes"]), kept["attempts"])
        self.stop(relay)

        # Restart against the same state directory: the journal is all that survives.
        relay = self.start(agents)
        status, gone = relay.request("GET", f"/v1/workflows/{workflow_id}")
        self.assertEqual(status, 404)
        self.assertIn("resume", gone["message"])

        status, resumed = relay.request("POST", f"/v1/workflows/{workflow_id}/resume")
        self.assertEqual(status, 202)
        done = wait(relay, workflow_id)
        self.assertEqual(done["status"], "completed")
        finished = steps_of(done)
        self.assertEqual(finished["first"]["status"], "completed")
        self.assertEqual(finished["first"]["output"], "kept")
        self.assertEqual(finished["first"]["attempts"], 1)
        self.assertEqual(finished["first"]["taskIds"], [])
        self.assertEqual(finished["first"]["startedAt"], kept["startedAt"])
        self.assertEqual(finished["first"]["finishedAt"], kept["finishedAt"])
        self.assertEqual(finished["first"]["attemptTimes"], kept["attemptTimes"])
        self.assertEqual(finished["second"]["output"], "recovered")

    def test_a_resumed_run_keeps_its_request_id(self):
        mark = self.path("resume-request-mark")
        relay = self.start({"id": "flaky", "command": PYTHON, "args": ["-c", FLAKY],
                            "env": {"WF_MARK": mark}})
        spec = {"name": "resume-id", "requestId": "resume-1",
                "steps": [{"id": "only", "agentId": "flaky", "input": "go"}]}
        status, submitted = relay.request("POST", "/v1/workflows", spec)
        self.assertEqual(status, 202)
        self.assertEqual(wait(relay, submitted["id"])["status"], "failed")

        status, _ = relay.request("POST", f"/v1/workflows/{submitted['id']}/resume")
        self.assertEqual(status, 202)
        # Replaying the original submission must find the resumed run, not the dead one.
        status, again = relay.request("POST", "/v1/workflows", spec)
        self.assertEqual(status, 200)
        self.assertEqual(again["id"], submitted["id"])
        self.assertIn(again["status"], ("queued", "running", "completed"))

    def test_resume_reports_an_unknown_run(self):
        relay = self.start(echo())
        status, body = relay.request("POST", "/v1/workflows/wf-00000000000000000000000000000000/resume")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "unknown_workflow")

    # ------------------------------------------------------------------ idempotency

    def test_returns_the_same_run_for_a_reused_request_id(self):
        relay = self.start(echo())
        spec = {"name": "idem", "requestId": "run-1",
                "steps": [{"id": "only", "agentId": "echo", "input": "once"}]}
        status, first = relay.request("POST", "/v1/workflows", spec)
        self.assertEqual(status, 202)
        status, again = relay.request("POST", "/v1/workflows", spec)
        self.assertEqual(status, 200)
        self.assertEqual(again["id"], first["id"])

        conflict = dict(spec, name="different")
        status, body = relay.request("POST", "/v1/workflows", conflict)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "idempotency_conflict")

    # ------------------------------------------------------------------ validation

    def test_rejects_malformed_workflows_with_a_specific_error(self):
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
                                 {"id": "b", "agentId": "echo", "input": "y",
                                  "needs": ["a"]}]}, "cyclic_dependency"),
            ("reference_to_nothing", {"steps": [{**base, "input": "{{ghost.output}}"}]},
             "unknown_reference"),
            ("reference_without_a_dependency",
             {"steps": [base, {"id": "b", "agentId": "echo", "input": "{{a.output}}"}]},
             "unknown_reference"),
            ("item_without_foreach", {"steps": [{**base, "input": "{{item}}"}]},
             "unknown_reference"),
            ("malformed_placeholder", {"steps": [{**base, "input": "{{a.output"}]},
             "invalid_template"),
            ("foreach_too_wide",
             {"steps": [{"id": "a", "agentId": "echo", "input": "x",
                         "foreach": [str(i) for i in range(64)]}]}, "invalid_foreach"),
            ("repeat_with_foreach",
             {"steps": [{**base, "foreach": ["x"],
                         "repeat": {"max": 2, "untilOutputMatches": "done"}}]},
             "invalid_repeat"),
            ("repeat_bad_regex",
             {"steps": [{**base, "repeat": {"max": 2, "untilOutputMatches": "("}}]},
             "invalid_repeat"),
            ("zero_timeout", {"steps": [{**base, "timeoutMs": 0}]}, "invalid_timeout"),
            ("bad_cwd", {"steps": [{**base, "cwd": ""}]}, "invalid_cwd"),
            ("too_many_retries", {"steps": [{**base, "retries": 99}]}, "invalid_retries"),
            ("bad_options", {"steps": [{**base, "options": ["effort"]}]}, "invalid_options"),
            ("bad_option_value", {"steps": [{**base, "options": {"effort": 1}}]},
             "invalid_options"),
            ("non_boolean_read_only", {"steps": [{**base, "readOnly": "yes"}]},
             "invalid_read_only"),
        ]
        relay = self.start(echo())
        for name, spec, expected in cases:
            with self.subTest(name):
                status, body = relay.request("POST", "/v1/workflows", spec)
                self.assertEqual(status, 400, body)
                self.assertEqual(body["error"], expected)

        _, listing = relay.request("GET", "/v1/workflows")
        self.assertEqual(listing["workflows"], [])

    def test_reports_an_unknown_workflow_and_lists_runs(self):
        relay = self.start(echo())
        status, body = relay.request("GET", "/v1/workflows/wf-deadbeef000000000000000000000000")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "unknown_workflow")

        run(relay, {"name": "listed", "steps": [dict(id="a", agentId="echo", input="x")]})
        _, listing = relay.request("GET", "/v1/workflows?limit=10")
        self.assertEqual(len(listing["workflows"]), 1)
        self.assertNotIn("output", listing["workflows"][0]["steps"][0])

        _, filtered = relay.request("GET", "/v1/workflows?status=completed")
        self.assertEqual(len(filtered["workflows"]), 1)
        _, empty = relay.request("GET", "/v1/workflows?status=failed")
        self.assertEqual(empty["workflows"], [])

        status, body = relay.request("GET", "/v1/workflows?status=whatever")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_status")

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

    def test_refuses_more_steps_than_the_limit(self):
        relay = self.start(echo(), env={"AGENT_RELAY_MAX_STEPS": "2"})
        self.assertEqual(relay.request("GET", "/healthz")[1]["limits"]["maxSteps"], 2)
        status, body = relay.request("POST", "/v1/workflows", {"steps": [
            {"id": "a", "agentId": "echo", "input": "1"},
            {"id": "b", "agentId": "echo", "input": "2"},
            {"id": "c", "agentId": "echo", "input": "3"}]})
        self.assertEqual((status, body["error"]), (400, "invalid_steps"))

    def test_advertises_workflow_limits_on_healthz(self):
        relay = self.start(echo())
        _, health = relay.request("GET", "/healthz")
        self.assertEqual(health["limits"]["maxSteps"], 64)
        self.assertEqual(health["limits"]["maxForEach"], 32)

    def test_rejects_a_step_that_points_its_cwd_outside_the_allowed_roots(self):
        relay = self.start({"id": "rooted", "command": PYTHON, "args": ["-c", ECHO],
                            "allowedRoots": [self.data]})
        status, body = relay.request("POST", "/v1/workflows", {"steps": [
            {"id": "a", "agentId": "rooted", "input": "x", "cwd": tempfile.gettempdir() + "/.."},
        ]})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "cwd_not_allowed")


if __name__ == "__main__":
    unittest.main()
