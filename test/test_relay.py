"""HTTP relay behaviour tests."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_helpers import ROOT, PYTHON, Relay, SERVER, start_http_server, stop_http_server


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
        self.assertTrue(text.startswith("Configuration error: Invalid command agent: bad"), text)
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


if __name__ == "__main__":
    unittest.main()
