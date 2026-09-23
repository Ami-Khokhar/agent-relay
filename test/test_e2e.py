"""End-to-end: MCP client -> relay HTTP API -> hosted HTTP harness."""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_helpers import McpClient, Relay, send_json, start_http_server, stop_http_server


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


if __name__ == "__main__":
    unittest.main()
