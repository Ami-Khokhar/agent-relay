"""Task lifecycle tests: deadlines, cancellation, and active-slot accounting."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from relay_helpers import PYTHON, Relay, send_json, start_http_server, stop_http_server
import server

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

    def test_a_detached_descendant_holding_stdin_cannot_pin_the_task(self):
        # The adapter exits at once; a child in its own session inherits stdin and never reads it.
        script = ("import subprocess, sys; subprocess.Popen([sys.executable, '-c', "
                  "'import time; time.sleep(8)'], start_new_session=True)")
        relay = Relay({"id": "leaky", "type": "stdio", "command": PYTHON, "args": ["-c", script]},
                      env={"A2A_RELAY_MAX_ACTIVE": "1"})
        try:
            _, submitted = relay.submit(agentId="leaky", input=LARGE_INPUT)
            task = relay.wait_task(submitted["id"], timeout=6)
            self.assertEqual(task["status"], "failed")
            self.assertTrue(_wait_for(lambda: self.active(relay) == 0))
        finally:
            relay.close()

    def test_a_detached_descendant_holding_stdout_cannot_pin_the_task(self):
        script = ("import subprocess, sys; subprocess.Popen([sys.executable, '-c', "
                  "'import time; time.sleep(8)'], start_new_session=True); print('done')")
        relay = Relay({"id": "daemonizes", "command": PYTHON, "args": ["-c", script]})
        try:
            _, submitted = relay.submit(agentId="daemonizes", input="x")
            task = relay.wait_task(submitted["id"], timeout=6)
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["output"], "done")
            self.assertTrue(_wait_for(lambda: self.active(relay) == 0))
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


    def test_http_deadline_bounds_a_response_that_drips_bytes(self):
        stop = []

        def handler(request):
            request.rfile.read(int(request.headers.get("content-length", 0)))
            request.send_response(200)
            request.send_header("content-type", "text/plain")
            request.end_headers()
            for _ in range(50):  # one byte every 0.1 s for 5 s
                if stop:
                    return
                try:
                    request.wfile.write(b"x")
                    request.wfile.flush()
                except OSError:
                    return
                time.sleep(0.1)

        http_server, port = start_http_server(handler)
        relay = Relay({"id": "drip", "type": "http", "url": f"http://127.0.0.1:{port}"})
        try:
            started = time.monotonic()
            _, submitted = relay.submit(agentId="drip", input="x", timeoutMs=500)
            task = relay.wait_task(submitted["id"], timeout=3)
            self.assertEqual(task["status"], "timed_out")
            self.assertLess(time.monotonic() - started, 3)
            self.assertTrue(_wait_for(lambda: self.active(relay) == 0))
        finally:
            stop.append(True)
            relay.close()
            stop_http_server(http_server)

    def _assert_foreign_group_survives(self, recorded):
        foreign = subprocess.Popen([PYTHON, "-c", "import time; time.sleep(30)"], start_new_session=True)
        self.addCleanup(foreign.wait)
        self.addCleanup(foreign.kill)
        # A reaped adapter leader whose pid (and so group id) now belongs to another group.
        reaped = types.SimpleNamespace(pid=foreign.pid, returncode=0, relay_start_time=recorded)
        server._signal(reaped, force=True)
        server._signal(reaped, force=False)
        time.sleep(0.2)
        self.assertIsNone(foreign.poll(), "a foreign process group was signalled")

    @unittest.skipUnless(server.PROCESS_GROUPS, "process groups are POSIX-only")
    def test_a_reaped_leaders_group_is_not_signalled_when_it_cannot_be_verified(self):
        # No start time was recorded (no /proc, e.g. macOS): the relay cannot tell whose it is.
        self._assert_foreign_group_survives(None)

    @unittest.skipUnless(server.PROCESS_GROUPS and os.path.exists(f"/proc/{os.getpid()}/stat"),
                         "needs /proc start times")
    def test_a_reaped_leaders_pid_held_by_a_foreign_group_is_not_signalled(self):
        self._assert_foreign_group_survives(b"not-the-foreign-start-time")

    @unittest.skipUnless(server.PROCESS_GROUPS and os.path.exists(f"/proc/{os.getpid()}/stat"),
                         "needs /proc start times")
    def test_a_reaped_leaders_own_surviving_group_is_still_signalled(self):
        child_pid = self.path("child.pid")
        script = ("import os, subprocess, sys\n"
                  "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                  "open(os.environ['CHILD_PID'], 'w').write(str(child.pid))\n")
        leader = subprocess.Popen([PYTHON, "-c", script], start_new_session=True,
                                  env={**os.environ, "CHILD_PID": child_pid})
        leader.relay_start_time = server._start_time(leader.pid)
        leader.wait()
        with open(child_pid, encoding="utf-8") as handle:
            pid = int(handle.read())
        server._signal(leader, force=True)

        def gone():
            # A killed orphan may linger as a zombie where nothing reaps it; that still counts.
            try:
                with open(f"/proc/{pid}/stat", "rb") as handle:
                    return handle.read().rsplit(b")", 1)[1].split()[0] == b"Z"
            except (OSError, IndexError):
                return True

        self.assertTrue(_wait_for(gone), "a descendant of the reaped leader survived")

    def _http_error_task(self, handler, timeout_ms):
        http_server, port = start_http_server(handler)
        relay = Relay({"id": "failing", "type": "http", "url": f"http://127.0.0.1:{port}"})
        try:
            started = time.monotonic()
            _, submitted = relay.submit(agentId="failing", input="x", timeoutMs=timeout_ms)
            task = relay.wait_task(submitted["id"], timeout=6)
            return task, time.monotonic() - started
        finally:
            relay.close()
            stop_http_server(http_server)

    def test_http_error_body_that_stalls_times_out_at_the_deadline(self):
        def handler(request):
            request.rfile.read(int(request.headers.get("content-length", 0)))
            time.sleep(1.0)  # spend most of the deadline before the headers arrive
            request.send_response(500)
            request.send_header("content-type", "text/plain")
            request.send_header("content-length", "100")
            request.end_headers()
            request.wfile.write(b"partial")
            request.wfile.flush()
            time.sleep(4)

        task, elapsed = self._http_error_task(handler, 1500)
        self.assertEqual(task["status"], "timed_out")
        # The socket timeout must be reset to what is left, not the 1.5 s set at request time.
        self.assertLess(elapsed, 2.3)

    def test_http_error_body_that_drips_times_out(self):
        def handler(request):
            request.rfile.read(int(request.headers.get("content-length", 0)))
            request.send_response(500)
            request.send_header("content-type", "text/plain")
            request.end_headers()
            for _ in range(40):
                try:
                    request.wfile.write(b"x")
                    request.wfile.flush()
                except OSError:
                    return
                time.sleep(0.1)

        task, elapsed = self._http_error_task(handler, 500)
        self.assertEqual(task["status"], "timed_out")
        self.assertLess(elapsed, 3)

    def test_http_error_body_cut_short_fails(self):
        def handler(request):
            request.rfile.read(int(request.headers.get("content-length", 0)))
            request.send_response(500)
            request.send_header("content-type", "text/plain")
            request.send_header("content-length", "100")
            request.end_headers()
            request.wfile.write(b"short")
            request.wfile.flush()
            request.close_connection = True

        task, _ = self._http_error_task(handler, 5000)
        self.assertEqual(task["status"], "failed")
        # The truncated body is reported as the adapter's text, never as a complete result.
        self.assertEqual(task["error"], "HTTP agent returned 500")
        self.assertEqual(task.get("output"), "short")

if __name__ == "__main__":
    unittest.main()
