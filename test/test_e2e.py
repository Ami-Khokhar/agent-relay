"""End-to-end: MCP client -> relay HTTP API -> hosted HTTP harness."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_helpers import PYTHON, McpClient, Relay, send_json, start_http_server, stop_http_server


class EndToEndTests(unittest.TestCase):
    def test_delegates_through_mcp_and_the_relay_to_a_harness_on_its_own_http_port(self):
        received = {}

        def handler(request):
            length = int(request.headers.get("content-length", 0))
            received.update(json.loads(request.rfile.read(length).decode("utf-8")))
            send_json(request, 200, {
                "protocolVersion": "relay.adapter/v1",
                "status": "completed",
                "output": f"fake harness completed: {received['task']['input']}",
            })

        server, port = start_http_server(handler)
        relay = Relay({"id": "portable-harness", "name": "Portable fake harness",
                       "type": "http", "url": f"http://127.0.0.1:{port}/tasks"})
        client = McpClient(relay.base)
        try:
            initialized = client.call("initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "e2e-test", "version": "1"},
            })
            self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")

            agents = client.call_tool("list_agents", {})["structuredContent"]
            self.assertEqual(agents["agents"][0]["id"], "portable-harness")
            self.assertEqual(agents["agents"][0]["adapter"], "http")

            delegated = client.call_tool("delegate", {
                "agentId": "portable-harness", "input": "review this change", "requestId": "e2e-request-1",
            })["structuredContent"]
            task_id = delegated["id"]
            self.assertTrue(task_id)

            task = client.call_tool("wait_task", {"taskId": task_id, "maxWaitMs": 5000})["structuredContent"]

            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["output"], "fake harness completed: review this change")
            self.assertEqual(received["protocolVersion"], "relay.adapter/v1")
            self.assertEqual(received["task"]["input"], "review this change")
            self.assertEqual(received["task"]["id"], task_id)
            self.assertEqual(received["task"]["sessionId"], task["sessionId"])
            self.assertEqual(received["task"]["timeoutMs"], task["timeoutMs"])
        finally:
            client.close()
            relay.close()
            stop_http_server(server)

    def test_wait_task_above_the_relay_limit_waits_the_limit_and_reports_the_expired_wait(self):
        relay = Relay({"id": "slow", "command": PYTHON, "args": ["-c", "import time; time.sleep(30)"]},
                      env={"A2A_RELAY_MAX_WAIT_MS": "500"})
        client = McpClient(relay.base)
        try:
            task_id = client.call_tool("delegate", {"agentId": "slow", "input": "x"})["structuredContent"]["id"]
            started = time.monotonic()
            result = client.call_tool("wait_task", {"taskId": task_id, "maxWaitMs": 900000})
            elapsed = time.monotonic() - started
            self.assertNotIn("isError", result)
            task = result["structuredContent"]
            self.assertIn(task["status"], ("queued", "running"))
            self.assertIs(task["waitExpired"], True)
            self.assertEqual(task["waitedMs"], 500)
            self.assertEqual(task["waitMs"], 500)
            self.assertIs(task["waitClamped"], True)
            self.assertIn("wait_task again with the same taskId", task["hint"])
            self.assertGreaterEqual(elapsed, 0.4)

            client.call_tool("cancel_task", {"taskId": task_id})
            done = client.call_tool("wait_task", {"taskId": task_id, "maxWaitMs": 500})["structuredContent"]
            self.assertEqual(done["status"], "cancelled")
            self.assertNotIn("waitExpired", done)
        finally:
            client.close()
            relay.close()

    def test_delegate_with_a_wait_above_the_relay_limit_creates_exactly_one_task(self):
        relay = Relay({"id": "slow", "command": PYTHON, "args": ["-c", "import time; time.sleep(30)"]},
                      env={"A2A_RELAY_MAX_WAIT_MS": "500"})
        client = McpClient(relay.base)
        try:
            arguments = {"agentId": "slow", "input": "x", "requestId": "once", "waitMs": 900000}
            result = client.call_tool("delegate", arguments)
            self.assertNotIn("isError", result)
            task = result["structuredContent"]
            self.assertTrue(task["id"])
            self.assertEqual(task["waitMs"], 500)
            self.assertIs(task["waitClamped"], True)
            self.assertIs(task["waitExpired"], True)
            self.assertEqual(task["waitedMs"], 500)

            # The refused first attempt created nothing, so one task exists, and a retry finds it.
            listed = client.call_tool("list_tasks", {})["structuredContent"]["tasks"]
            self.assertEqual([item["id"] for item in listed], [task["id"]])
            again = client.call_tool("delegate", arguments)["structuredContent"]
            self.assertEqual(again["id"], task["id"])
            listed = client.call_tool("list_tasks", {})["structuredContent"]["tasks"]
            self.assertEqual(len(listed), 1)
            client.call_tool("cancel_task", {"taskId": task["id"]})
        finally:
            client.close()
            relay.close()


    def test_mcp_results_leave_out_the_input_unless_get_task_asks_for_it(self):
        text = "review this change"
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print('done')"]})
        client = McpClient(relay.base)
        try:
            delegated = client.call_tool("delegate", {
                "agentId": "echo", "input": text, "waitMs": 5000})["structuredContent"]
            task_id = delegated["id"]
            waited = client.call_tool("wait_task", {"taskId": task_id})["structuredContent"]
            fetched = client.call_tool("get_task", {"taskId": task_id})["structuredContent"]
            listed = client.call_tool("list_tasks", {})["structuredContent"]["tasks"][0]
            self.assertEqual(delegated["status"], "completed")
            for snapshot in (delegated, waited, fetched, listed):
                self.assertNotIn("input", snapshot)
                self.assertEqual(snapshot["inputChars"], len(text))
            opted_in = client.call_tool("get_task", {"taskId": task_id, "includeInput": True})
            self.assertEqual(opted_in["structuredContent"]["input"], text)
        finally:
            client.close()
            relay.close()


if __name__ == "__main__":
    unittest.main()
