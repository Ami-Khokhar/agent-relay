"""HTTP relay behaviour tests."""
from __future__ import annotations

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_helpers import ROOT, PYTHON, Relay, SERVER, start_http_server, stop_http_server

sys.path.insert(0, os.path.join(ROOT, "src"))
from server import _WARNED_FALLBACK, default_config_path


class RelayTests(unittest.TestCase):
    def test_submits_and_polls_a_command_task_with_explicit_session_identity(self):
        relay = Relay({"id": "echo", "command": PYTHON,
                       "args": ["-c", 'import os,sys; print(os.environ["A2A_SESSION_ID"] + ":" + sys.argv[1])']})
        try:
            status, submitted = relay.submit(agentId="echo", sessionId="session-7", input="hello")
            self.assertEqual(status, 202)
            self.assertRegex(submitted["id"], r"^[0-9a-f-]{36}$")
            self.assertEqual(submitted["sessionId"], "session-7")
            task = relay.wait_task(submitted["id"])
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["output"], "session-7:hello")
        finally:
            relay.close()

    def test_times_out_command_tasks(self):
        relay = Relay({"id": "slow", "command": PYTHON, "args": ["-c", "import time; time.sleep(10)"]},
                      env={"A2A_RELAY_TIMEOUT_MS": "50"})
        try:
            self.assertEqual(relay.run_task(agentId="slow", input="wait")["status"], "timed_out")
        finally:
            relay.close()

    def test_cancels_tasks_and_bounds_captured_output(self):
        relay = Relay({"id": "output", "command": PYTHON,
                       "args": ["-c", 'import sys,time; sys.stdout.write("x"*5000); time.sleep(10)']},
                      env={"A2A_RELAY_MAX_OUTPUT_BYTES": "32"})
        try:
            _, first = relay.submit(agentId="output", input="x")
            time.sleep(0.05)
            status, cancelled = relay.request("DELETE", f"/v1/tasks/{first['id']}")
            self.assertEqual(status, 200)
            self.assertEqual(cancelled["status"], "cancelled")
        finally:
            relay.close()

        quick = Relay({"id": "output", "command": PYTHON,
                       "args": ["-c", 'import sys; sys.stdout.write("x"*5000)']},
                      env={"A2A_RELAY_MAX_OUTPUT_BYTES": "32"})
        try:
            bounded = quick.run_task(agentId="output", input="x")
            self.assertEqual(len(bounded["output"].encode("utf-8")), 32)
            self.assertTrue(bounded["outputTruncated"])
        finally:
            quick.close()

    def test_queues_at_the_concurrency_limit_and_evicts_the_oldest_terminal_task(self):
        relay = Relay({"id": "work", "command": PYTHON,
                       "args": ["-c", "import sys,time; time.sleep(0.1); print(sys.argv[1])"]},
                      env={"A2A_RELAY_MAX_ACTIVE": "1", "A2A_RELAY_MAX_TASKS": "2"})
        try:
            _, first = relay.submit(agentId="work", input="one")
            _, second = relay.submit(agentId="work", input="two")
            time.sleep(0.03)
            _, second_now = relay.request("GET", f"/v1/tasks/{second['id']}")
            self.assertEqual(second_now["status"], "queued")
            relay.wait_task(first["id"])
            _, third = relay.submit(agentId="work", input="three")
            self.assertTrue(third["id"])
            status, _ = relay.request("GET", f"/v1/tasks/{first['id']}")
            self.assertEqual(status, 404)
        finally:
            relay.close()

    def test_rejects_invalid_task_timeout_and_does_not_inherit_unrelated_environment(self):
        relay = Relay({"id": "env", "command": PYTHON,
                       "args": ["-c", 'import os; print(os.environ.get("RELAY_TEST_SECRET", "clean"))']},
                      env={"RELAY_TEST_SECRET": "must-not-leak"})
        try:
            status, _ = relay.submit(agentId="env", input="x", timeoutMs=-1)
            self.assertEqual(status, 400)
            self.assertEqual(relay.run_task(agentId="env", input="x")["output"], "clean")
        finally:
            relay.close()

        opted_in = Relay({"id": "env", "command": PYTHON,
                          "args": ["-c", 'import os; print(os.environ["RELAY_TEST_SECRET"])'],
                          "inheritEnv": ["RELAY_TEST_SECRET"]},
                         env={"RELAY_TEST_SECRET": "available"})
        try:
            self.assertEqual(opted_in.run_task(agentId="env", input="x")["output"], "available")
        finally:
            opted_in.close()

    def test_reports_invalid_registry_configuration_without_a_stack_trace(self):
        directory = tempfile.mkdtemp(prefix="a2a-invalid-")
        config = os.path.join(directory, "agents.json")
        with open(config, "w", encoding="utf-8") as handle:
            json.dump({"agents": [{"id": "bad", "command": PYTHON, "args": "wrong", "env": {"TOKEN": 3}}]}, handle)
        proc = subprocess.Popen([PYTHON, SERVER], cwd=ROOT,
                                env={**os.environ, "A2A_AGENTS_FILE": config},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 1)
        text = stderr.decode("utf-8")
        self.assertRegex(text, r"(?m)^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00 "
                               r"Configuration error: Invalid command agent: bad")
        self.assertNotIn("Traceback (most recent call last)", text)

    def test_returns_413_for_oversized_requests_and_command_arguments(self):
        body_limited = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print(1)"]},
                             env={"A2A_RELAY_MAX_BODY_BYTES": "64"})
        try:
            status, _ = body_limited.submit(agentId="echo", input="x" * 100)
            self.assertEqual(status, 413)
        finally:
            body_limited.close()

        argument_limited = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print(1)"]},
                                 env={"A2A_RELAY_MAX_COMMAND_INPUT_BYTES": "8"})
        try:
            status, payload = argument_limited.submit(agentId="echo", input="x" * 9)
            self.assertEqual(status, 413)
            self.assertEqual(payload["error"], "command_input_too_large")
        finally:
            argument_limited.close()

    def test_deduplicates_task_submission_by_request_id_and_rejects_conflicting_reuse(self):
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "import sys; print(sys.argv[1])"]})
        try:
            status_a, first = relay.submit(agentId="echo", requestId="caller-1", input="one")
            status_b, replay = relay.submit(agentId="echo", requestId="caller-1", input="one")
            self.assertEqual(status_a, 202)
            self.assertEqual(status_b, 200)
            self.assertEqual(replay["id"], first["id"])
            status_c, conflict = relay.submit(agentId="echo", requestId="caller-1", input="two")
            self.assertEqual(status_c, 409)
            self.assertEqual(conflict["error"], "idempotency_conflict")
        finally:
            relay.close()

    def test_task_snapshots_leave_out_the_input_and_report_its_length(self):
        text = "naïve prompt ✓"
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print('ok')"]})
        try:
            status, submitted = relay.submit(agentId="echo", requestId="snap-1", input=text)
            self.assertEqual(status, 202)
            status, replay = relay.submit(agentId="echo", requestId="snap-1", input=text)
            self.assertEqual(status, 200)
            task_id = submitted["id"]
            relay.wait_task(task_id)
            _, polled = relay.request("GET", f"/v1/tasks/{task_id}")
            _, waited = relay.request("GET", f"/v1/tasks/{task_id}?waitMs=5000")
            _, listing = relay.request("GET", "/v1/tasks")
            _, cancelled = relay.request("DELETE", f"/v1/tasks/{task_id}")
            for snapshot in (submitted, replay, polled, waited, listing["tasks"][0], cancelled):
                self.assertNotIn("input", snapshot)
                self.assertEqual(snapshot["inputChars"], len(text))
            self.assertEqual(polled["output"], "ok")
        finally:
            relay.close()

    def test_get_task_returns_the_input_only_when_include_input_is_true(self):
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print('ok')"]})
        try:
            task = relay.run_task(agentId="echo", input="secret prompt")
            path = f"/v1/tasks/{task['id']}"
            status, opted_in = relay.request("GET", path + "?includeInput=true")
            self.assertEqual(status, 200)
            self.assertEqual(opted_in["input"], "secret prompt")
            self.assertEqual(opted_in["inputChars"], len("secret prompt"))
            _, waited = relay.request("GET", path + "?waitMs=1000&includeInput=true")
            self.assertEqual(waited["input"], "secret prompt")
            _, opted_out = relay.request("GET", path + "?includeInput=false")
            self.assertNotIn("input", opted_out)
            status, payload = relay.request("GET", path + "?includeInput=maybe")
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"], "invalid_include_input")
            # The list endpoint never carries inputs, whatever the query says.
            _, listing = relay.request("GET", "/v1/tasks?includeInput=true")
            self.assertNotIn("input", listing["tasks"][0])
        finally:
            relay.close()

    def test_terminates_running_agent_processes_when_the_relay_shuts_down(self):
        directory = tempfile.mkdtemp(prefix="a2a-shutdown-")
        marker = os.path.join(directory, "killed")
        ready = os.path.join(directory, "ready")
        script = (
            "import os, signal, time\n"
            "def handler(signum, frame):\n"
            "    open(os.environ['MARKER'], 'w').write('killed')\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, handler)\n"
            "open(os.environ['READY'], 'w').write('ready')\n"
            "time.sleep(60)\n"
        )
        relay = Relay({"id": "sleeper", "command": PYTHON, "args": ["-c", script],
                       "env": {"MARKER": marker, "READY": ready}})
        try:
            _, submitted = relay.submit(agentId="sleeper", input="wait")
            deadline = time.time() + 5
            while time.time() < deadline and not os.path.exists(ready):
                time.sleep(0.02)
            _, task = relay.request("GET", f"/v1/tasks/{submitted['id']}")
            self.assertEqual(task["status"], "running")
            relay.proc.send_signal(signal.SIGTERM)
            deadline = time.time() + 5
            while time.time() < deadline and not os.path.exists(marker):
                time.sleep(0.02)
            self.assertTrue(os.path.exists(marker))
        finally:
            relay.close()

    def test_returns_405_for_known_routes_with_the_wrong_method(self):
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print(1)"]})
        try:
            for method, path in [("PUT", "/v1/tasks"), ("POST", "/v1/agents"),
                                 ("DELETE", "/healthz"), ("POST", "/v1/tasks/unknown-id")]:
                status, payload = relay.request(method, path)
                self.assertEqual(status, 405, f"{method} {path}")
                self.assertEqual(payload["error"], "method_not_allowed")
        finally:
            relay.close()

    def test_bounds_http_adapter_responses_while_streaming(self):
        def handler(request):
            request.send_response(200)
            request.send_header("content-type", "text/plain")
            request.end_headers()
            request.wfile.write(b"x" * 24)
            request.wfile.flush()
            request.wfile.write(b"y" * 24)
            request.wfile.flush()

        server, port = start_http_server(handler)
        relay = Relay({"id": "remote", "type": "http", "url": f"http://127.0.0.1:{port}"},
                      env={"A2A_RELAY_MAX_OUTPUT_BYTES": "32"})
        try:
            completed = relay.run_task(agentId="remote", input="work")
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(len(completed["output"].encode("utf-8")), 32)
            self.assertTrue(completed["outputTruncated"])
        finally:
            relay.close()
            stop_http_server(server)

    def test_applies_per_agent_timeout_above_the_global_default(self):
        relay = Relay({"id": "slow", "command": PYTHON, "args": ["-c", "print('ok')"],
                       "timeoutMs": 900000}, env={"A2A_RELAY_TIMEOUT_MS": "1000"})
        try:
            task = relay.run_task(agentId="slow", input="x")
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["timeoutMs"], 900000)
        finally:
            relay.close()

    def test_request_timeout_overrides_the_agent_and_the_default(self):
        relay = Relay({"id": "slow", "command": PYTHON, "args": ["-c", "print('ok')"],
                       "timeoutMs": 5000}, env={"A2A_RELAY_TIMEOUT_MS": "1000"})
        try:
            task = relay.run_task(agentId="slow", input="x", timeoutMs=7000)
            self.assertEqual(task["timeoutMs"], 7000)
        finally:
            relay.close()

    def test_default_timeout_is_fifteen_minutes(self):
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print('ok')"]})
        try:
            _, submitted = relay.submit(agentId="echo", input="x")
            self.assertEqual(submitted["timeoutMs"], 900000)
        finally:
            relay.close()

    def test_rejects_an_agent_timeout_above_the_configured_max(self):
        directory = tempfile.mkdtemp(prefix="a2a-maxtimeout-")
        config = os.path.join(directory, "agents.json")
        with open(config, "w", encoding="utf-8") as handle:
            json.dump({"agents": [{"id": "slow", "command": PYTHON,
                                   "args": ["-c", "print(1)"], "timeoutMs": 5000}]}, handle)
        proc = subprocess.Popen([PYTHON, SERVER], cwd=ROOT,
                                env={**os.environ, "A2A_AGENTS_FILE": config,
                                     "A2A_RELAY_MAX_TIMEOUT_MS": "1000"},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 1)
        text = stderr.decode("utf-8")
        self.assertIn("timeoutMs exceeds", text)
        self.assertNotIn("Traceback (most recent call last)", text)

    def test_rejects_a_request_timeout_above_the_configured_max(self):
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print('ok')"]},
                      env={"A2A_RELAY_MAX_TIMEOUT_MS": "1000"})
        try:
            status, payload = relay.submit(agentId="echo", input="x", timeoutMs=2000)
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"], "invalid_timeout")
        finally:
            relay.close()

    def test_allows_per_task_cwd_only_inside_allowed_roots(self):
        allowed = tempfile.mkdtemp(prefix="a2a-allowed-")
        outside = tempfile.mkdtemp(prefix="a2a-outside-")
        relay = Relay({"id": "pwd", "command": PYTHON,
                       "args": ["-c", "import os; print(os.getcwd())"],
                       "allowedRoots": [allowed]})
        try:
            status, submitted = relay.submit(agentId="pwd", input="x", cwd=allowed)
            self.assertEqual(status, 202)
            self.assertEqual(submitted["cwd"], os.path.realpath(allowed))
            task = relay.wait_task(submitted["id"])
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["output"], os.path.realpath(allowed))

            status, payload = relay.submit(agentId="pwd", input="x", cwd=outside)
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"], "cwd_not_allowed")

            status, payload = relay.submit(agentId="pwd", input="x", cwd="/tmp")
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"], "cwd_not_allowed")
        finally:
            relay.close()

    def test_rejects_unknown_task_request_fields(self):
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print('ok')"]})
        try:
            status, payload = relay.submit(agentId="echo", input="x", unsupported=True)
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"], "unknown_field")
            self.assertIn("unsupported", payload["message"])
        finally:
            relay.close()

    def test_long_polls_until_a_task_is_terminal(self):
        relay = Relay({"id": "later", "command": PYTHON,
                       "args": ["-c", "import time; time.sleep(0.3); print('done')"]})
        try:
            _, submitted = relay.submit(agentId="later", input="x")
            started = time.monotonic()
            status, task = relay.request("GET", f"/v1/tasks/{submitted['id']}?waitMs=5000")
            elapsed = time.monotonic() - started
            self.assertEqual(status, 200)
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["output"], "done")
            self.assertGreaterEqual(elapsed, 0.25)

            status, payload = relay.request("GET", f"/v1/tasks/{submitted['id']}?waitMs=0")
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"], "invalid_wait")
        finally:
            relay.close()

    def test_a_terminal_wait_result_does_not_carry_waitExpired(self):
        relay = Relay({"id": "quick", "command": PYTHON, "args": ["-c", "print('done')"]})
        try:
            _, submitted = relay.submit(agentId="quick", input="x")
            status, task = relay.request("GET", f"/v1/tasks/{submitted['id']}?waitMs=5000")
            self.assertEqual((status, task["status"]), (200, "completed"))
            self.assertNotIn("waitExpired", task)
            self.assertNotIn("waitedMs", task)
            self.assertNotIn("hint", task)

            # A task that is already terminal when the wait starts is terminal too.
            status, task = relay.request("GET", f"/v1/tasks/{submitted['id']}?waitMs=5000")
            self.assertEqual((status, task["status"]), (200, "completed"))
            self.assertNotIn("waitExpired", task)
        finally:
            relay.close()

    def test_a_wait_that_ends_before_the_task_is_terminal_says_so(self):
        relay = Relay({"id": "slow", "command": PYTHON, "args": ["-c", "import time; time.sleep(30)"]})
        try:
            _, submitted = relay.submit(agentId="slow", input="x")
            started = time.monotonic()
            status, task = relay.request("GET", f"/v1/tasks/{submitted['id']}?waitMs=300")
            elapsed = time.monotonic() - started
            self.assertEqual(status, 200)
            self.assertIn(task["status"], ("queued", "running"))
            self.assertIs(task["waitExpired"], True)
            self.assertEqual(task["waitedMs"], 300)
            self.assertIn("wait_task again with the same taskId", task["hint"])
            self.assertGreaterEqual(elapsed, 0.25)

            # The marker describes this wait only. It is not stored on the task.
            status, plain = relay.request("GET", f"/v1/tasks/{submitted['id']}")
            self.assertEqual(status, 200)
            self.assertNotIn("waitExpired", plain)
            relay.request("DELETE", f"/v1/tasks/{submitted['id']}")
        finally:
            relay.close()

    def test_an_invalid_wait_error_names_the_valid_range(self):
        relay = Relay({"id": "quick", "command": PYTHON, "args": ["-c", "print('done')"]},
                      env={"A2A_RELAY_MAX_WAIT_MS": "2000"})
        try:
            _, submitted = relay.submit(agentId="quick", input="x")
            for value in ("2001", "900000", "0", "-5", "soon"):
                status, payload = relay.request("GET", f"/v1/tasks/{submitted['id']}?waitMs={value}")
                self.assertEqual(status, 400, value)
                self.assertEqual(payload["error"], "invalid_wait", value)
                self.assertIn("1 to 2000", payload["message"], value)
            status, _ = relay.request("GET", f"/v1/tasks/{submitted['id']}?waitMs=2000")
            self.assertEqual(status, 200)
        finally:
            relay.close()

    def test_a_submission_with_an_invalid_wait_is_refused_before_it_creates_a_task(self):
        relay = Relay({"id": "quick", "command": PYTHON, "args": ["-c", "print('done')"]},
                      env={"A2A_RELAY_MAX_WAIT_MS": "2000"})
        try:
            for value in (2001, 900000, 0, -5, "soon", 1.5, True):
                status, payload = relay.submit(agentId="quick", input="x", requestId="r-1", waitMs=value)
                self.assertEqual(status, 400, value)
                self.assertEqual(payload["error"], "invalid_wait", value)
                self.assertIn("1 to 2000", payload["message"], value)
            status, listed = relay.request("GET", "/v1/tasks")
            self.assertEqual((status, listed["tasks"]), (200, []))

            # The refusals left nothing behind: the same requestId still makes one new task.
            status, task = relay.submit(agentId="quick", input="x", requestId="r-1", waitMs=2000)
            self.assertEqual(status, 202)
            status, replay = relay.submit(agentId="quick", input="x", requestId="r-1", waitMs=2000)
            self.assertEqual((status, replay["id"]), (200, task["id"]))
            status, listed = relay.request("GET", "/v1/tasks")
            self.assertEqual([item["id"] for item in listed["tasks"]], [task["id"]])
            self.assertNotIn("waitMs", task)
        finally:
            relay.close()

    def test_a_stored_replay_ignores_an_invalid_advisory_wait(self):
        relay = Relay({"id": "quick", "command": PYTHON, "args": ["-c", "print('done')"]},
                      env={"A2A_RELAY_MAX_WAIT_MS": "2000"})
        try:
            status, task = relay.submit(agentId="quick", input="x", requestId="r-1", waitMs=2000)
            self.assertEqual(status, 202)
            # The wait is advisory and never stored, so a replay that repeats every stored field
            # answers with the stored task even when its wait alone would be refused for one.
            status, replay = relay.submit(agentId="quick", input="x", requestId="r-1", waitMs=900000)
            self.assertEqual((status, replay["id"]), (200, task["id"]))
            # Identity still decides a changed replay: the invalid wait does not mask the conflict.
            status, conflict = relay.submit(agentId="quick", input="changed",
                                            requestId="r-1", waitMs=900000)
            self.assertEqual((status, conflict["error"]), (409, "idempotency_conflict"))
            status, listed = relay.request("GET", "/v1/tasks")
            self.assertEqual([item["id"] for item in listed["tasks"]], [task["id"]])
        finally:
            relay.close()

    def test_lists_tasks_by_session_and_rejects_a_bad_status(self):
        relay = Relay({"id": "echo", "command": PYTHON,
                       "args": ["-c", "import sys; print(sys.argv[1])"]})
        try:
            relay.submit(agentId="echo", input="one", sessionId="s-1")
            relay.submit(agentId="echo", input="two", sessionId="s-1")
            relay.submit(agentId="echo", input="three", sessionId="s-2")
            status, listing = relay.request("GET", "/v1/tasks?sessionId=s-1")
            self.assertEqual(status, 200)
            self.assertEqual(len(listing["tasks"]), 2)
            self.assertTrue(all(task["sessionId"] == "s-1" for task in listing["tasks"]))

            status, payload = relay.request("GET", "/v1/tasks?status=bogus")
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"], "invalid_status")
        finally:
            relay.close()

    def test_healthz_reports_the_registry_file_path(self):
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print(input())"]})
        try:
            status, health = relay.request("GET", "/healthz")
            self.assertEqual(status, 200)
            self.assertEqual(health["registry"], relay.config)
        finally:
            relay.close()

    def test_reports_effective_limits_and_agent_timeouts(self):
        relay = Relay({"id": "slow", "command": PYTHON, "args": ["-c", "print(1)"],
                       "timeoutMs": 60000}, env={"A2A_RELAY_MAX_ACTIVE": "2"})
        try:
            status, health = relay.request("GET", "/healthz")
            self.assertEqual(status, 200)
            self.assertEqual(health["limits"]["timeoutMs"], 900000)
            self.assertEqual(health["limits"]["maxActive"], 2)
            _, listing = relay.request("GET", "/v1/agents")
            self.assertEqual(listing["agents"][0]["timeoutMs"], 60000)
        finally:
            relay.close()

    def test_reloads_the_registry_without_a_restart(self):
        relay = Relay({"id": "one", "command": PYTHON, "args": ["-c", "print(1)"]})
        try:
            with open(relay.config, "w", encoding="utf-8") as handle:
                json.dump({"agents": [
                    {"id": "one", "command": PYTHON, "args": ["-c", "print(1)"]},
                    {"id": "two", "command": PYTHON, "args": ["-c", "print(2)"]},
                ]}, handle)
            status, payload = relay.request("POST", "/v1/admin/reload")
            self.assertEqual(status, 200)
            self.assertEqual(payload["agents"], 2)
            _, listing = relay.request("GET", "/v1/agents")
            self.assertEqual({agent["id"] for agent in listing["agents"]}, {"one", "two"})
        finally:
            relay.close()

    def test_reports_port_in_use_without_a_traceback(self):
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print(1)"]})
        try:
            proc = subprocess.Popen([PYTHON, SERVER], cwd=ROOT,
                                    env={**os.environ, "A2A_RELAY_PORT": str(relay.port),
                                         "A2A_AGENTS_FILE": relay.config},
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            _, stderr = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 1)
            text = stderr.decode("utf-8")
            self.assertIn("in use", text)
            self.assertNotIn("Traceback (most recent call last)", text)
        finally:
            relay.close()


class RelayLogLineTests(unittest.TestCase):
    """Every relay log line starts with a UTC ISO 8601 time and names the ids it stopped."""

    def test_a_cancelled_task_and_a_stopped_run_are_named_in_the_timestamped_log(self):
        directory = tempfile.mkdtemp(prefix="a2a-logline-")
        self.addCleanup(shutil.rmtree, directory, True)
        state = tempfile.mkdtemp(prefix="a2a-logline-state-")
        self.addCleanup(shutil.rmtree, state, True)
        # A stub host that starts calm and turns critical when the test says so, so the
        # submission preflight passes and later the guard stops the running workflow.
        level_file = os.path.join(directory, "level")
        with open(level_file, "w", encoding="utf-8") as handle:
            handle.write("0\n")
        probes = {
            "sysctl": (
                '#!/bin/sh\ncase "$2" in\n'
                f'  vm.memory_pressure) cat "{level_file}" ;;\n'
                "  kern.memorystatus_level) echo 60 ;;\n"
                '  vm.swapusage) echo "total = 2048.00M  used = 100.00M  free = 1948.00M'
                '  (encrypted)" ;;\n'
                "  *) exit 1 ;;\nesac\n"
            ),
            "pmset": '#!/bin/sh\necho "Note: No thermal warning level has been recorded"\n',
        }
        env = {}
        for name, body in probes.items():
            path = os.path.join(directory, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(body)
            os.chmod(path, 0o755)
            env["AGENT_RELAY_" + name.upper()] = path
        relay = Relay({"id": "slow", "command": PYTHON, "args": ["-c", "import time; time.sleep(30)"]},
                      env={**env, "AGENT_RELAY_PRESSURE_GUARD": "1",
                           "AGENT_RELAY_PRESSURE_INTERVAL_MS": "100",
                           "AGENT_RELAY_PRESSURE_HOLD_MS": "0",
                           "AGENT_RELAY_STATE_DIR": state})
        try:
            _, submitted = relay.request("POST", "/v1/workflows", {
                "steps": [{"id": "wait", "agentId": "slow", "input": "long job"}]})
            workflow_id = submitted["id"]
            self.wait_step_running(relay, workflow_id)
            _, task = relay.submit(agentId="slow", input="standalone")
            task_id = task["id"]
            self.wait_task_running(relay, task_id)

            with open(level_file, "w", encoding="utf-8") as handle:
                handle.write("2\n")
            deadline = time.time() + 15
            while time.time() < deadline:
                _, view = relay.request("GET", f"/v1/workflows/{workflow_id}")
                if view["status"] == "cancelled":
                    break
                time.sleep(0.02)
            else:
                self.fail("the guard never stopped the workflow")

            relay.proc.send_signal(signal.SIGTERM)
            relay.proc.wait(timeout=10)
            lines = relay.proc.stdout.read().decode("utf-8").splitlines()
        finally:
            relay.close()

        for line in lines:
            if line:
                self.assertRegex(line, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00 ", line)
        startup = next(line for line in lines if "agent-relay listening at" in line)
        self.assertTrue(startup.endswith(f"(registry: {relay.config})"), startup)
        stopped = next(line for line in lines if "device pressure critical" in line)
        self.assertIn(workflow_id, stopped)
        cancelled = next(line for line in lines if "Received SIGTERM" in line)
        self.assertIn(task_id, cancelled)
        self.assertIn("cancelling 1 running task(s)", cancelled)

    def wait_step_running(self, relay, workflow_id, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, view = relay.request("GET", f"/v1/workflows/{workflow_id}")
            if view["steps"][0]["status"] == "running":
                return
            time.sleep(0.02)
        self.fail("the workflow step never started")

    def wait_task_running(self, relay, task_id, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, view = relay.request("GET", f"/v1/tasks/{task_id}")
            if view["status"] == "running":
                return
            time.sleep(0.02)
        self.fail("the task never started")


class DefaultConfigPathTests(unittest.TestCase):
    def test_warns_on_stderr_when_falling_back_to_checkout_registry(self):
        with tempfile.TemporaryDirectory() as home:
            stderr = io.StringIO()
            with mock.patch("pathlib.Path.home", return_value=Path(home)), redirect_stderr(stderr), \
                    mock.patch("server._WARNED_FALLBACK", False):
                path = default_config_path()
            self.assertEqual(path, (Path(ROOT) / "config" / "agents.json").resolve())
            self.assertIn(f"falling back to {path}", stderr.getvalue())

    def test_warns_on_fallback_only_once_per_process(self):
        with tempfile.TemporaryDirectory() as home:
            stderr = io.StringIO()
            with mock.patch("pathlib.Path.home", return_value=Path(home)), redirect_stderr(stderr), \
                    mock.patch("server._WARNED_FALLBACK", False):
                path = default_config_path()
                self.assertEqual(path, (Path(ROOT) / "config" / "agents.json").resolve())
                default_config_path()
            self.assertEqual(stderr.getvalue().count("falling back"), 1)

    def test_silent_when_user_registry_exists(self):
        with tempfile.TemporaryDirectory() as home:
            registry_dir = Path(home) / ".config" / "agent-relay"
            registry_dir.mkdir(parents=True)
            (registry_dir / "agents.json").write_text("{}", encoding="utf-8")
            stderr = io.StringIO()
            with mock.patch("pathlib.Path.home", return_value=Path(home)), redirect_stderr(stderr):
                path = default_config_path()
            self.assertEqual(path, registry_dir / "agents.json")
            self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
