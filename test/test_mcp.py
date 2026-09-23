"""MCP interface tests: tool mapping, error mapping, validation, and stdio transport."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from relay_helpers import MCP_SERVER, PYTHON, ROOT, send_json, start_http_server, stop_http_server
import mcp_server


class FakeUpstream:
    """A fake relay that records calls and replies via a responder callback."""

    def __init__(self, responder):
        self.calls = []
        self.responder = responder

        def handler(request):
            length = int(request.headers.get("content-length", 0))
            body = json.loads(request.rfile.read(length).decode("utf-8")) if length else None
            self.calls.append((request.command, request.path, body))
            status, payload = self.responder(request.command, request.path, body)
            send_json(request, status, payload)

        self.server, self.port = start_http_server(handler)
        self.url = f"http://127.0.0.1:{self.port}"

    def close(self):
        stop_http_server(self.server)


class McpHandlerTests(unittest.TestCase):
    def test_maps_mcp_tools_onto_the_http_task_lifecycle(self):
        def responder(method, path, body):
            if path.endswith("/v1/agents"):
                return 200, {"agents": [{"id": "fake", "adapter": "command"}]}
            if path.endswith("/v1/tasks") and method == "POST":
                return 202, {"id": "task-1", "sessionId": "session-1", "agentId": "fake", "status": "queued"}
            if method == "DELETE":
                return 200, {"id": "task-1", "status": "cancelled"}
            return 200, {"id": "task-1", "status": "completed", "output": "fake result"}

        upstream = FakeUpstream(responder)
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            listed = handle({"method": "tools/list"})
            self.assertEqual([tool["name"] for tool in listed["tools"]],
                             ["list_agents", "delegate", "wait_task", "get_task", "list_tasks",
                              "cancel_task"])
            agents = handle({"method": "tools/call", "params": {"name": "list_agents", "arguments": {}}})
            self.assertEqual(agents["structuredContent"]["agents"][0]["id"], "fake")
            submitted = handle({"method": "tools/call", "params": {
                "name": "delegate",
                "arguments": {"agentId": "fake", "input": "do work", "sessionId": "session-1", "requestId": "request-1"},
            }})
            self.assertEqual(submitted["structuredContent"]["id"], "task-1")
            self.assertEqual(upstream.calls[1][2],
                             {"agentId": "fake", "input": "do work", "sessionId": "session-1", "requestId": "request-1"})
            completed = handle({"method": "tools/call", "params": {"name": "get_task", "arguments": {"taskId": "task-1"}}})
            self.assertEqual(completed["structuredContent"]["output"], "fake result")
            cancelled = handle({"method": "tools/call", "params": {"name": "cancel_task", "arguments": {"taskId": "task-1"}}})
            self.assertEqual(cancelled["structuredContent"]["status"], "cancelled")
            self.assertEqual(upstream.calls[3][0], "DELETE")
        finally:
            upstream.close()

    def test_returns_relay_failures_as_mcp_tool_errors(self):
        upstream = FakeUpstream(lambda method, path, body: (404, {"error": "unknown_task"}))
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            result = handle({"method": "tools/call", "params": {"name": "get_task", "arguments": {"taskId": "missing"}}})
            self.assertTrue(result["isError"])
            self.assertEqual(result["structuredContent"],
                             {"error": "unknown_task", "message": "unknown_task", "status": 404})
        finally:
            upstream.close()

        malformed = FakeUpstream(lambda method, path, body: (502, "bad gateway"))
        handle = mcp_server.create_http_handler(malformed.url, timeout=5)
        try:
            result = handle({"method": "tools/call", "params": {"name": "list_agents", "arguments": {}}})
            self.assertTrue(result["isError"])
            self.assertEqual(result["structuredContent"],
                             {"error": "invalid_relay_response", "message": "invalid_relay_response", "status": 502})
        finally:
            malformed.close()

    def test_maps_wait_task_and_list_tasks_onto_the_relay(self):
        def responder(method, path, body):
            if path.startswith("/v1/tasks?"):
                return 200, {"tasks": [{"id": "task-1", "status": "completed"}]}
            return 200, {"id": "task-1", "status": "completed", "output": "done"}

        upstream = FakeUpstream(responder)
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            waited = handle({"method": "tools/call", "params": {
                "name": "wait_task", "arguments": {"taskId": "task-1", "maxWaitMs": 5000}}})
            self.assertEqual(waited["structuredContent"]["output"], "done")
            self.assertEqual(upstream.calls[0][0], "GET")
            self.assertIn("waitMs=5000", upstream.calls[0][1])

            listed = handle({"method": "tools/call", "params": {
                "name": "list_tasks",
                "arguments": {"sessionId": "s-1", "status": "completed", "limit": 5}}})
            self.assertEqual(listed["structuredContent"]["tasks"][0]["id"], "task-1")
            self.assertIn("sessionId=s-1", upstream.calls[1][1])
            self.assertIn("status=completed", upstream.calls[1][1])
            self.assertIn("limit=5", upstream.calls[1][1])
        finally:
            upstream.close()

    def test_delegate_passes_cwd_and_can_wait_for_the_result(self):
        def responder(method, path, body):
            if method == "POST":
                return 202, {"id": "task-2", "status": "queued"}
            return 200, {"id": "task-2", "status": "completed", "output": "ok"}

        upstream = FakeUpstream(responder)
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            result = handle({"method": "tools/call", "params": {
                "name": "delegate",
                "arguments": {"agentId": "fake", "input": "work", "cwd": "/tmp", "waitMs": 3000}}})
            self.assertEqual(result["structuredContent"]["status"], "completed")
            self.assertEqual(upstream.calls[0][2], {"agentId": "fake", "input": "work", "cwd": "/tmp"})
            self.assertIn("waitMs=3000", upstream.calls[1][1])
        finally:
            upstream.close()

    def test_retries_a_loopback_request_after_starting_the_relay(self):
        calls = []

        def fake_request(base_url, path, method="GET", body=None, timeout=10.0):
            calls.append(path)
            if len(calls) == 1:
                raise mcp_server.RelayError(None, {"error": "relay_request_failed"}, "refused")
            return {"ok": True}

        original_request = mcp_server._http_request
        original_start = mcp_server._maybe_start_relay
        mcp_server._http_request = fake_request
        mcp_server._maybe_start_relay = lambda base_url: True
        try:
            value = mcp_server._relay_request("http://127.0.0.1:1", "/healthz")
            self.assertEqual(value, {"ok": True})
            self.assertEqual(calls, ["/healthz", "/healthz"])
        finally:
            mcp_server._http_request = original_request
            mcp_server._maybe_start_relay = original_start

    def test_validates_tool_arguments_before_calling_the_relay(self):
        upstream = FakeUpstream(lambda method, path, body: (200, {}))
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            with self.assertRaises(mcp_server.RpcError) as first:
                handle({"method": "tools/call", "params": {
                    "name": "delegate", "arguments": {"agentId": "fake", "input": "", "extra": True}}})
            self.assertEqual(first.exception.code, -32602)
            with self.assertRaises(mcp_server.RpcError) as second:
                handle({"method": "tools/call", "params": {
                    "name": "delegate", "arguments": {"agentId": "fake", "input": "work", "timeoutMs": 1.5}}})
            self.assertEqual(second.exception.code, -32602)
            self.assertEqual(len(upstream.calls), 0)
        finally:
            upstream.close()


class McpServeTests(unittest.TestCase):
    def test_stdio_reports_malformed_requests_and_does_not_serialize_independent_calls(self):
        class QueueInput:
            def __init__(self):
                self.items = queue.Queue()

            def feed(self, line):
                self.items.put(line)

            def close(self):
                self.items.put(None)

            def __iter__(self):
                return self

            def __next__(self):
                item = self.items.get()
                if item is None:
                    raise StopIteration
                return item

        class ListOutput:
            def __init__(self):
                self.lines = []

            def write(self, text):
                self.lines.append(text)

            def flush(self):
                pass

            def parsed(self):
                return [json.loads(line) for line in "".join(self.lines).splitlines() if line.strip()]

        slow_started = threading.Event()
        slow_release = threading.Event()

        def handle(rpc):
            if rpc["method"] == "slow":
                slow_started.set()
                slow_release.wait(5)
                return {"done": True}
            return {"pong": True}

        input_stream, output_stream = QueueInput(), ListOutput()
        runner = threading.Thread(target=mcp_server.serve, args=(input_stream, output_stream, handle), daemon=True)
        runner.start()
        input_stream.feed("null\n")
        input_stream.feed("{bad json\n")
        input_stream.feed(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "slow"}) + "\n")
        self.assertTrue(slow_started.wait(5))
        input_stream.feed(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n")
        deadline = time.time() + 5
        while time.time() < deadline and not any(m.get("id") == 2 for m in output_stream.parsed()):
            time.sleep(0.01)
        early = output_stream.parsed()
        self.assertTrue(any(m.get("id") == 2 for m in early))
        self.assertFalse(any(m.get("id") == 1 for m in early))
        self.assertEqual(early[0]["error"]["code"], -32600)
        self.assertEqual(early[1]["error"]["code"], -32700)
        slow_release.set()
        input_stream.close()
        runner.join(5)
        self.assertTrue(any(m.get("id") == 1 for m in output_stream.parsed()))

    def test_executable_entry_point_serves_stdio(self):
        proc = subprocess.Popen([PYTHON, MCP_SERVER], cwd=ROOT,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            request = {"jsonrpc": "2.0", "id": 7, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "fake", "version": "1"}}}
            proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
            proc.stdin.flush()
            response = json.loads(proc.stdout.readline())
            self.assertEqual(response["id"], 7)
            self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()


if __name__ == "__main__":
    unittest.main()
