"""Task retention: stored prompts and results are dropped after the configured period."""
from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_helpers import PYTHON, Relay


class RetentionTests(unittest.TestCase):
    def test_drops_terminal_tasks_after_the_retention_period(self):
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "import sys; print(sys.argv[1])"]},
                      env={"AGENT_RELAY_TASK_RETENTION_MS": "300"})
        try:
            status, health = relay.request("GET", "/healthz")
            self.assertEqual(health["limits"]["taskRetentionMs"], 300)
            _, submitted = relay.submit(agentId="echo", input="secret prompt", requestId="r-1")
            self.assertEqual(relay.wait_task(submitted["id"])["output"], "secret prompt")
            time.sleep(0.5)
            status, _ = relay.request("GET", f"/v1/tasks/{submitted['id']}")
            self.assertEqual(status, 404)
            self.assertEqual(relay.request("GET", "/v1/tasks")[1], {"tasks": []})
            # The idempotency key is released with the task, so a retry starts fresh work.
            status, again = relay.submit(agentId="echo", input="secret prompt", requestId="r-1")
            self.assertEqual(status, 202)
            self.assertNotEqual(again["id"], submitted["id"])
        finally:
            relay.close()

    def test_purges_expired_tasks_on_an_idle_relay(self):
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print('ok')"]},
                      env={"AGENT_RELAY_TASK_RETENTION_MS": "300"})
        try:
            relay.run_task(agentId="echo", input="secret prompt")
            # /healthz does not purge, so only the background sweep can empty the store.
            self.assertEqual(relay.request("GET", "/healthz")[1]["tasks"], 1)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and relay.request("GET", "/healthz")[1]["tasks"]:
                time.sleep(0.05)
            self.assertEqual(relay.request("GET", "/healthz")[1]["tasks"], 0)
        finally:
            relay.close()

    def test_keeps_terminal_tasks_by_default(self):
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print('ok')"]})
        try:
            self.assertIsNone(relay.request("GET", "/healthz")[1]["limits"]["taskRetentionMs"])
            task = relay.run_task(agentId="echo", input="x")
            time.sleep(0.3)
            self.assertEqual(relay.request("GET", f"/v1/tasks/{task['id']}")[0], 200)
        finally:
            relay.close()


if __name__ == "__main__":
    unittest.main()
