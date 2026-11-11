"""POST /v1/tasks honours waitMs by long-polling, like GET /v1/tasks/:id?waitMs= does."""
import unittest

from relay_helpers import Relay


class PostTasksWaitTests(unittest.TestCase):
    def test_post_tasks_honours_wait_ms_or_refuses_it(self):
        """A submission with waitMs blocks until the task is terminal, and a wait the relay
        cannot honour is refused with a 400 naming GET /v1/tasks/:id?waitMs= instead of
        being silently ignored."""
        relay = Relay({"id": "quick", "command": "python3", "args": ["-c", "print('done')"]},
                      env={"A2A_RELAY_MAX_WAIT_MS": "2000"})
        try:
            # A fast task: the POST itself returns the terminal task, not a queued one.
            status, task = relay.submit(agentId="quick", input="x", waitMs=2000)
            self.assertEqual(status, 200)
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["output"], "done")
            self.assertNotIn("waitExpired", task)

            # A wait the relay cannot honour is refused before it creates a task, and the
            # refusal names the long poll that waits on an existing task instead.
            status, payload = relay.submit(agentId="quick", input="x", requestId="r-1",
                                           waitMs=999999)
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"], "invalid_wait")
            self.assertIn("GET /v1/tasks/:id?waitMs=", payload["message"])
            # The refusals left nothing behind: only the completed task from above exists.
            status, listed = relay.request("GET", "/v1/tasks")
            self.assertEqual(status, 200)
            self.assertEqual([t["status"] for t in listed["tasks"]], ["completed"])
        finally:
            relay.close()

    def test_a_wait_above_the_ceiling_is_clamped(self):
        """A wait above A2A_RELAY_MAX_WAIT_MS is never honoured past the ceiling: the relay
        refuses it with 400 invalid_wait (naming the range) before it creates a task, so
        nothing runs without an ID."""
        relay = Relay({"id": "quick", "command": "python3", "args": ["-c", "print('done')"]},
                      env={"A2A_RELAY_MAX_WAIT_MS": "2000"})
        try:
            for value in (2001, 900000):
                status, payload = relay.submit(agentId="quick", input="x", waitMs=value)
                self.assertEqual(status, 400, value)
                self.assertEqual(payload["error"], "invalid_wait", value)
                self.assertIn("1 to 2000", payload["message"], value)
            status, listed = relay.request("GET", "/v1/tasks")
            self.assertEqual((status, listed["tasks"]), (200, []))
        finally:
            relay.close()


if __name__ == "__main__":
    unittest.main()
