"""A task that waits in the queue while the owner reloads the registry runs as the registry now says."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from relay_helpers import PRINT_ARGV, PYTHON, Relay, send_json, start_http_server, stop_http_server

SANDBOX = sys.platform == "darwin" and os.access("/usr/bin/sandbox-exec", os.X_OK)
# Holds its active slot until the file named in its input exists.
HOLD = "import os, sys, time\nwhile not os.path.exists(sys.argv[1]): time.sleep(0.02)\n"
BLOCKER = {"id": "blocker", "command": PYTHON, "args": ["-c", HOLD]}
EFFORT = {"low": ["--low"], "high": ["--high"]}


def probe(**extra):
    return {"id": "probe", "command": PYTHON, "args": [*PRINT_ARGV, "--base"], **extra}


class QueuedTaskReloadTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="a2a-queued-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.gate = os.path.join(self.dir, "gate")
        self.hits = []

        def handler(request):
            length = int(request.headers.get("content-length") or 0)
            self.hits.append(request.rfile.read(length))
            send_json(request, 200, {"protocolVersion": "relay.adapter/v1", "status": "completed",
                                     "output": "ran over http"})

        server, port = start_http_server(handler)
        self.addCleanup(stop_http_server, server)
        self.http_agent = {"id": "probe", "type": "http", "url": f"http://127.0.0.1:{port}/run"}

    def queued_task_after_reload(self, before, after, **body):
        """Queue a task behind a busy relay, replace the registry with ``after``, then let the task dispatch."""
        relay = Relay([BLOCKER, before], env={"A2A_RELAY_MAX_ACTIVE": "1"})
        self.addCleanup(relay.close)
        _, first = relay.submit(agentId="blocker", input=self.gate)
        status, queued = relay.submit(agentId="probe", input="review", **body)
        self.assertEqual((status, queued["status"]), (202, "queued"))
        with open(relay.config, "w", encoding="utf-8") as handle:
            json.dump({"agents": [BLOCKER, after]}, handle)
        self.assertEqual(relay.request("POST", "/v1/admin/reload")[0], 200)
        open(self.gate, "w").close()
        self.assertEqual(relay.wait_task(first["id"])["status"], "completed")
        return relay.wait_task(queued["id"])

    def assert_failed_with(self, task, code):
        self.assertEqual(task["status"], "failed", task)
        self.assertTrue(task["error"].startswith(code + ":"), task)
        self.assertEqual(self.hits, [], "the task reached its agent")

    def test_a_queued_read_only_task_is_not_run_over_http_after_its_agent_became_an_http_adapter(self):
        task = self.queued_task_after_reload(
            probe(readOnly={"args": ["--no-write"]}), self.http_agent, readOnly=True)
        self.assert_failed_with(task, "read_only_unsupported")
        self.assertIs(task["readOnly"], True)

    def test_a_queued_read_only_task_is_not_run_after_its_agent_lost_the_profile(self):
        task = self.queued_task_after_reload(probe(readOnly={"args": ["--no-write"]}), probe(), readOnly=True)
        self.assert_failed_with(task, "read_only_unsupported")

    def test_a_queued_read_only_task_runs_with_the_profile_args_the_registry_now_has(self):
        task = self.queued_task_after_reload(
            probe(readOnly={"args": ["--old"]}), probe(readOnly={"args": ["--new"]}), readOnly=True)
        self.assertEqual(task["status"], "completed", task)
        self.assertEqual(json.loads(task["output"]), ["--base", "--new", "review"])

    def test_a_queued_task_runs_with_the_option_args_the_registry_now_maps(self):
        task = self.queued_task_after_reload(
            probe(options={"effort": EFFORT}), probe(options={"effort": {"high": ["--very-high"]}}),
            options={"effort": "high"})
        self.assertEqual(task["status"], "completed", task)
        self.assertEqual(json.loads(task["output"]), ["--base", "--very-high", "review"])

    def test_a_queued_task_fails_when_its_option_value_is_no_longer_allowed(self):
        task = self.queued_task_after_reload(
            probe(options={"effort": EFFORT}), probe(options={"effort": {"low": ["--low"]}}),
            options={"effort": "high"})
        self.assert_failed_with(task, "invalid_option_value")

    def test_a_queued_task_fails_when_its_option_is_no_longer_declared(self):
        for after in (probe(), probe(options={"mode": {"fast": ["--fast"]}}), self.http_agent):
            self.hits.clear()
            task = self.queued_task_after_reload(probe(options={"effort": EFFORT}), after,
                                                 options={"effort": "high"})
            self.assert_failed_with(task, "unknown_option")

    @unittest.skipUnless(SANDBOX, "a sandboxed read-only profile needs sandbox-exec (macOS)")
    def test_a_queued_sandboxed_task_fails_when_the_agent_no_longer_has_a_cwd_to_protect(self):
        task = self.queued_task_after_reload(
            probe(cwd=self.dir, readOnly={"sandbox": True}), probe(readOnly={"sandbox": True}), readOnly=True)
        self.assert_failed_with(task, "cwd_required")

    def test_a_queued_task_for_an_agent_that_is_still_registered_is_untouched(self):
        task = self.queued_task_after_reload(
            probe(options={"effort": EFFORT}, readOnly={"args": ["--no-write"]}),
            probe(options={"effort": EFFORT}, readOnly={"args": ["--no-write"]}),
            options={"effort": "high"}, readOnly=True)
        self.assertEqual(task["status"], "completed", task)
        self.assertEqual(json.loads(task["output"]), ["--base", "--high", "--no-write", "review"])


if __name__ == "__main__":
    unittest.main()
