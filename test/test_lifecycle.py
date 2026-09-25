"""Task lifecycle tests: deadlines, cancellation, and active-slot accounting."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_helpers import PYTHON, Relay, send_json, start_http_server, stop_http_server

# Larger than any default pipe buffer (64 KiB on Linux, at most 64 KiB on macOS).
LARGE_INPUT = "x" * 300_000
NEVER_READS_STDIN = "import time; time.sleep(30)"
IGNORES_SIGTERM = (
    "import os, signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "open(os.environ['READY'], 'w').write('ready')\n"
    "time.sleep(30)\n"
)


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="a2a-lifecycle-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def path(self, name):
        return os.path.join(self.dir, name)

    def active(self, relay):
        return relay.request("GET", "/healthz")[1]["active"]

    def test_stdio_timeout_applies_while_the_request_is_still_being_written(self):
        relay = Relay({"id": "deaf", "type": "stdio", "command": PYTHON,
                       "args": ["-c", NEVER_READS_STDIN]})
        try:
            started = time.monotonic()
            status, submitted = relay.submit(agentId="deaf", input=LARGE_INPUT, timeoutMs=200)
            self.assertEqual(status, 202)
            task = relay.wait_task(submitted["id"], timeout=5)
            self.assertEqual(task["status"], "timed_out")
            self.assertLess(time.monotonic() - started, 5)
            self.assertTrue(_wait_for(lambda: self.active(relay) == 0))
        finally:
            relay.close()

    def test_cancel_interrupts_a_blocked_stdin_write_and_frees_the_slot(self):
        relay = Relay({"id": "deaf", "type": "stdio", "command": PYTHON,
                       "args": ["-c", NEVER_READS_STDIN]}, env={"A2A_RELAY_MAX_ACTIVE": "1"})
        try:
            _, blocked = relay.submit(agentId="deaf", input=LARGE_INPUT)
            time.sleep(0.3)
            status, cancelled = relay.request("DELETE", f"/v1/tasks/{blocked['id']}")
            self.assertEqual(status, 200)
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertEqual(cancelled["cancellation"], "process_signal")
            self.assertTrue(_wait_for(lambda: self.active(relay) == 0))
        finally:
            relay.close()

    def test_cancelled_process_keeps_its_slot_until_it_exits(self):
        ready = self.path("ready")
        relay = Relay({"id": "stubborn", "command": PYTHON, "args": ["-c", IGNORES_SIGTERM],
                       "env": {"READY": ready}}, env={"A2A_RELAY_MAX_ACTIVE": "1"})
        try:
            _, first = relay.submit(agentId="stubborn", input="x")
            self.assertTrue(_wait_for(lambda: os.path.exists(ready)))
            relay.request("DELETE", f"/v1/tasks/{first['id']}")
            _, second = relay.submit(agentId="stubborn", input="y")
            # SIGTERM is ignored, so the first process runs until SIGKILL about 1 s later.
            self.assertEqual(self.active(relay), 1)
            self.assertEqual(relay.request("GET", f"/v1/tasks/{second['id']}")[1]["status"], "queued")
            self.assertTrue(_wait_for(
                lambda: relay.request("GET", f"/v1/tasks/{second['id']}")[1]["status"] == "running"))
            relay.request("DELETE", f"/v1/tasks/{second['id']}")
        finally:
            relay.close()

    def test_cancel_stops_processes_started_by_the_adapter(self):
        ready, marker = self.path("ready"), self.path("grandchild-ran")
        script = (
            "import os, subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import os, time; time.sleep(1); "
            "open(os.environ[\"MARKER\"], \"w\").write(\"x\")'])\n"
            "open(os.environ['READY'], 'w').write('ready')\n"
            "time.sleep(30)\n"
        )
        relay = Relay({"id": "parent", "command": PYTHON, "args": ["-c", script],
                       "env": {"READY": ready, "MARKER": marker}})
        try:
            _, submitted = relay.submit(agentId="parent", input="x")
            self.assertTrue(_wait_for(lambda: os.path.exists(ready)))
            relay.request("DELETE", f"/v1/tasks/{submitted['id']}")
            time.sleep(1.5)
            self.assertFalse(os.path.exists(marker), "descendant kept running after cancel")
        finally:
            relay.close()

    def test_cancel_immediately_after_submission_never_runs_the_agent(self):
        marker = self.path("ran")
        script = "import os, time; time.sleep(0.5); open(os.environ['MARKER'], 'a').write('x')"
        relay = Relay({"id": "late", "command": PYTHON, "args": ["-c", script],
                       "env": {"MARKER": marker}})
        try:
            for _ in range(5):
                _, submitted = relay.submit(agentId="late", input="x")
                _, cancelled = relay.request("DELETE", f"/v1/tasks/{submitted['id']}")
                self.assertEqual(cancelled["status"], "cancelled")
            self.assertTrue(_wait_for(lambda: self.active(relay) == 0))
            time.sleep(0.8)
            self.assertFalse(os.path.exists(marker), "a cancelled task's process ran to completion")
        finally:
            relay.close()

    def test_http_cancellation_is_reported_as_request_only(self):
        def handler(request):
            time.sleep(1)
            send_json(request, 200, {"protocolVersion": "relay.adapter/v1",
                                     "status": "completed", "output": "late"})

        server, port = start_http_server(handler)
        relay = Relay({"id": "hosted", "type": "http", "url": f"http://127.0.0.1:{port}"})
        try:
            _, submitted = relay.submit(agentId="hosted", input="x")
            time.sleep(0.1)
            _, cancelled = relay.request("DELETE", f"/v1/tasks/{submitted['id']}")
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertEqual(cancelled["cancellation"], "request_only")
            time.sleep(1.2)
            final = relay.request("GET", f"/v1/tasks/{submitted['id']}")[1]
            self.assertEqual(final["status"], "cancelled")
            self.assertNotIn("output", final)
        finally:
            relay.close()
            stop_http_server(server)


if __name__ == "__main__":
    unittest.main()
