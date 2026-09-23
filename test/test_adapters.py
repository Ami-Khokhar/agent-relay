"""Adapter contract tests for stdio and HTTP agents."""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_helpers import FAKE_ADAPTER, PYTHON, Relay, send_json, start_http_server, stop_http_server


class StdioAdapterTests(unittest.TestCase):
    def test_stdio_adapter_works_from_config_and_reports_capabilities(self):
        relay = Relay({"id": "any-harness", "type": "stdio", "command": PYTHON, "args": [FAKE_ADAPTER]})
        try:
            _, listing = relay.request("GET", "/v1/agents")
            self.assertEqual(
                listing["agents"][0]["capabilities"],
                {"newTasks": True, "nativeSessions": False, "streaming": False,
                 "cancellation": "process_signal"},
            )
            text = "first line\n" + ("é" * 5000) + "\nlast line"
            task = relay.run_task(agentId="any-harness", sessionId="correlation-1", input=text)
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["output"], f"correlation-1:{text}")
        finally:
            relay.close()

    def test_stdio_adapter_normalizes_reported_malformed_and_oversized_failures(self):
        cases = [
            ("failed", "fake failure"),
            ("malformed", "invalid JSON"),
            ("oversized", "exceeded output limit"),
            ("early-exit", "invalid JSON|stdin failed"),
        ]
        for mode, expected in cases:
            with self.subTest(mode=mode):
                env = {"A2A_RELAY_MAX_OUTPUT_BYTES": "128"} if mode == "oversized" else {}
                relay = Relay({"id": "fake", "type": "stdio", "command": PYTHON,
                               "args": [FAKE_ADAPTER], "env": {"FAKE_ADAPTER_MODE": mode}}, env=env)
                try:
                    task = relay.run_task(agentId="fake", input="work")
                    self.assertEqual(task["status"], "failed")
                    self.assertRegex(task["error"], expected)
                finally:
                    relay.close()


class HttpAdapterTests(unittest.TestCase):
    def test_hosted_http_adapters_use_the_same_request_and_result_envelopes(self):
        received = {}

        def handler(request):
            length = int(request.headers.get("content-length", 0))
            received.update(json.loads(request.rfile.read(length).decode("utf-8")))
            send_json(request, 200, {
                "protocolVersion": "relay.adapter/v1",
                "status": "completed",
                "output": f"hosted:{received['task']['input']}",
            })

        server, port = start_http_server(handler)
        relay = Relay({"id": "hosted", "type": "http", "url": f"http://127.0.0.1:{port}"})
        try:
            task = relay.run_task(agentId="hosted", input="work")
            self.assertEqual(received["protocolVersion"], "relay.adapter/v1")
            self.assertEqual(received["task"]["id"], task["id"])
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["output"], "hosted:work")
            _, listing = relay.request("GET", "/v1/agents")
            self.assertEqual(listing["agents"][0]["capabilities"]["cancellation"], "request_only")
        finally:
            relay.close()
            stop_http_server(server)


if __name__ == "__main__":
    unittest.main()
