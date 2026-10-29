"""MCP interface tests: tool mapping, error mapping, validation, and stdio transport."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from relay_helpers import MCP_SERVER, PYTHON, ROOT, send_json, start_http_server, stop_http_server
import mcp_server
import workflows


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
    def test_initialize_advertises_relay_instructions(self):
        handle = mcp_server.create_http_handler("http://127.0.0.1:1", timeout=1)
        result = handle({"method": "initialize"})
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        instructions = result["instructions"]
        self.assertTrue(instructions.strip())
        self.assertLess(len(instructions), 1024)
        for keyword in ("delegate", "wait_task", "list_agents", "pi", "codex", "sessionId",
                        "orchestrat", "fan out", "shell"):
            self.assertIn(keyword, instructions)

    def test_tool_descriptions_name_the_harnesses_and_their_arguments(self):
        handle = mcp_server.create_http_handler("http://127.0.0.1:1", timeout=1)
        tools = {tool["name"]: tool for tool in handle({"method": "tools/list"})["tools"]}
        delegate = tools["delegate"]["description"].lower()
        for keyword in ("orchestrat", "delegat", "pi", "codex", "opencode", "claude"):
            self.assertIn(keyword, delegate)
        properties = tools["delegate"]["inputSchema"]["properties"]
        for key in ("agentId", "input", "sessionId", "requestId", "timeoutMs", "cwd", "waitMs"):
            self.assertTrue(properties[key].get("description"), f"{key} has no description")
        self.assertIn("list_agents", properties["agentId"]["description"])
        session_description = properties["sessionId"]["description"].lower()
        self.assertIn("correlation", session_description)
        self.assertNotIn("continue", session_description)

    def test_delegate_description_says_an_invalid_wait_creates_no_task(self):
        handle = mcp_server.create_http_handler("http://127.0.0.1:1", timeout=1)
        tools = {tool["name"]: tool for tool in handle({"method": "tools/list"})["tools"]}
        wait = tools["delegate"]["inputSchema"]["properties"]["waitMs"]["description"]
        for text in ("A2A_RELAY_MAX_WAIT_MS", "clamp", "waitMs", "waitClamped", "waitExpired", "no task"):
            self.assertIn(text, wait)
        self.assertNotIn("still created", wait)

    def test_wait_task_description_says_how_to_loop_on_a_short_tool_call_budget(self):
        handle = mcp_server.create_http_handler("http://127.0.0.1:1", timeout=1)
        tools = {tool["name"]: tool for tool in handle({"method": "tools/list"})["tools"]}
        description = tools["wait_task"]["description"]
        for text in ("waitExpired", "waitedMs", "same taskId", "120 s", "110000"):
            self.assertIn(text, description)
        max_wait = tools["wait_task"]["inputSchema"]["properties"]["maxWaitMs"]["description"]
        for text in ("A2A_RELAY_MAX_WAIT_MS", "clamp", "waitMs", "waitClamped"):
            self.assertIn(text, max_wait)

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
                             ["prepare_feedback", "list_agents", "delegate", "wait_task", "get_task", "list_tasks",
                              "cancel_task", "run_workflow", "get_workflow",
                              "wait_workflow_events", "list_workflows", "cancel_workflow",
                              "resume_workflow", "get_pressure"])
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

    def test_get_task_asks_the_relay_for_the_input_only_when_include_input_is_true(self):
        upstream = FakeUpstream(lambda method, path, body: (200, {"id": "task-1", "status": "completed"}))
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            def get_task(**arguments):
                return handle({"method": "tools/call", "params": {
                    "name": "get_task", "arguments": {"taskId": "task-1", **arguments}}})

            get_task()
            get_task(includeInput=True)
            get_task(includeInput=False)
            self.assertEqual([call[1] for call in upstream.calls],
                             ["/v1/tasks/task-1", "/v1/tasks/task-1?includeInput=true", "/v1/tasks/task-1"])
            with self.assertRaises(mcp_server.RpcError) as bad:
                get_task(includeInput="yes")
            self.assertEqual(bad.exception.code, -32602)
            self.assertEqual(len(upstream.calls), 3)
            tools = {tool["name"]: tool for tool in handle({"method": "tools/list"})["tools"]}
            option = tools["get_task"]["inputSchema"]["properties"]["includeInput"]
            self.assertEqual(option["type"], "boolean")
            self.assertTrue(option["description"])
            for name in ("delegate", "wait_task", "get_task", "list_tasks"):
                self.assertIn("inputChars", tools[name]["description"])
        finally:
            upstream.close()

    def test_wait_task_clamps_a_wait_above_the_relay_limit_and_says_which_wait_it_used(self):
        def responder(method, path, body):
            if path == "/healthz":
                return 200, {"ok": True, "limits": {"maxWaitMs": 600000}}
            if "waitMs=900000" in path:
                return 400, {"error": "invalid_wait", "message": "waitMs must be an integer from 1 to 600000"}
            return 200, {"id": "task-1", "status": "completed", "output": "done"}

        upstream = FakeUpstream(responder)
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            result = handle({"method": "tools/call", "params": {
                "name": "wait_task", "arguments": {"taskId": "task-1", "maxWaitMs": 900000}}})
            self.assertNotIn("isError", result)
            task = result["structuredContent"]
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["waitMs"], 600000)
            self.assertIs(task["waitClamped"], True)
            self.assertEqual([call[1] for call in upstream.calls],
                             ["/v1/tasks/task-1?waitMs=900000", "/healthz",
                              "/v1/tasks/task-1?waitMs=600000"])

            # A wait inside the limit is sent as given, once, and the result is not annotated.
            upstream.calls.clear()
            result = handle({"method": "tools/call", "params": {
                "name": "wait_task", "arguments": {"taskId": "task-1", "maxWaitMs": 600000}}})
            self.assertNotIn("waitClamped", result["structuredContent"])
            self.assertNotIn("waitMs", result["structuredContent"])
            self.assertEqual([call[1] for call in upstream.calls], ["/v1/tasks/task-1?waitMs=600000"])
        finally:
            upstream.close()

    def test_run_workflow_clamps_a_wait_above_the_relay_limit(self):
        def responder(method, path, body):
            if path == "/healthz":
                return 200, {"ok": True, "limits": {"maxWaitMs": 600000}}
            if method == "POST" and path == "/v1/workflows":
                return 202, {"id": "wf-1", "status": "queued"}
            if "waitMs=900000" in path:
                return 400, {"error": "invalid_wait",
                             "message": "waitMs must be an integer from 1 to 600000"}
            return 200, {"id": "wf-1", "status": "completed", "steps": []}

        upstream = FakeUpstream(responder)
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            result = handle({"method": "tools/call", "params": {
                "name": "run_workflow",
                "arguments": {"spec": {"steps": []}, "waitMs": 900000}}})
            run = result["structuredContent"]
            self.assertEqual(run["status"], "completed")
            self.assertEqual(run["waitMs"], 600000)
            self.assertIs(run["waitClamped"], True)
            self.assertEqual([call[1] for call in upstream.calls],
                             ["/v1/workflows", "/v1/workflows/wf-1?waitMs=900000", "/healthz",
                              "/v1/workflows/wf-1?waitMs=600000"])
        finally:
            upstream.close()

    def test_get_workflow_clamps_a_wait_above_the_relay_limit(self):
        def responder(method, path, body):
            if path == "/healthz":
                return 200, {"ok": True, "limits": {"maxWaitMs": 600000}}
            if "waitMs=900000" in path:
                return 400, {"error": "invalid_wait",
                             "message": "waitMs must be an integer from 1 to 600000"}
            return 200, {"id": "wf-1", "status": "completed", "steps": []}

        upstream = FakeUpstream(responder)
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            result = handle({"method": "tools/call", "params": {
                "name": "get_workflow",
                "arguments": {"workflowId": "wf-1", "maxWaitMs": 900000}}})
            run = result["structuredContent"]
            self.assertEqual(run["waitMs"], 600000)
            self.assertIs(run["waitClamped"], True)
            self.assertEqual([call[1] for call in upstream.calls],
                             ["/v1/workflows/wf-1?waitMs=900000", "/healthz",
                              "/v1/workflows/wf-1?waitMs=600000"])
        finally:
            upstream.close()

    def test_the_step_schema_matches_the_engine(self):
        # The schema tells a client what the engine will accept; these are the engine's own limits,
        # so a constant that changes without the schema fails here instead of at a client's submit.
        tool = next(tool for tool in mcp_server.TOOLS if tool["name"] == "run_workflow")
        step = tool["inputSchema"]["properties"]["spec"]["properties"]["steps"]["items"]["properties"]
        self.assertEqual(step["id"]["pattern"], workflows.STEP_ID_RE.pattern)
        self.assertEqual(step["needs"]["items"]["pattern"], workflows.STEP_ID_RE.pattern)
        self.assertEqual(step["foreach"]["items"]["maxLength"], workflows.MAX_ITEM_CHARS)
        self.assertEqual(step["retries"]["maximum"], workflows.MAX_RETRIES)
        self.assertEqual(step["repeat"]["properties"]["max"]["maximum"], workflows.MAX_REPEAT_ROUNDS)

    def test_wait_task_returns_the_relay_error_when_it_cannot_clamp_the_wait(self):
        rejected = {"error": "invalid_wait", "message": "invalid_wait"}

        def old_relay(method, path, body):
            if path == "/healthz":
                return 200, {"ok": True}
            return 400, rejected

        def odd_relay(method, path, body):
            if path == "/healthz":
                return 200, {"ok": True, "limits": {"maxWaitMs": 900000}}
            return 400, rejected

        for responder in (old_relay, odd_relay):
            upstream = FakeUpstream(responder)
            handle = mcp_server.create_http_handler(upstream.url, timeout=5)
            try:
                result = handle({"method": "tools/call", "params": {
                    "name": "wait_task", "arguments": {"taskId": "task-1", "maxWaitMs": 600000}}})
                self.assertTrue(result["isError"])
                self.assertEqual(result["structuredContent"],
                                 {"error": "invalid_wait", "message": "invalid_wait", "status": 400})
                self.assertEqual([call[1] for call in upstream.calls],
                                 ["/v1/tasks/task-1?waitMs=600000", "/healthz"])
            finally:
                upstream.close()

        upstream = FakeUpstream(lambda method, path, body: (404, {"error": "unknown_task"}))
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            result = handle({"method": "tools/call", "params": {
                "name": "wait_task", "arguments": {"taskId": "missing", "maxWaitMs": 900000}}})
            self.assertEqual(result["structuredContent"]["error"], "unknown_task")
            self.assertEqual(len(upstream.calls), 1)
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
            # The wait goes with the submission, so the relay can refuse it before it creates the task.
            self.assertEqual(upstream.calls[0][2],
                             {"agentId": "fake", "input": "work", "cwd": "/tmp", "waitMs": 3000})
            self.assertIn("waitMs=3000", upstream.calls[1][1])
            self.assertNotIn("waitClamped", result["structuredContent"])
        finally:
            upstream.close()

    def test_delegate_clamps_a_wait_above_the_relay_limit_and_says_which_wait_it_used(self):
        def responder(method, path, body):
            if path == "/healthz":
                return 200, {"ok": True, "limits": {"maxWaitMs": 600000}}
            if method == "POST":
                if body["waitMs"] > 600000:
                    return 400, {"error": "invalid_wait",
                                 "message": "waitMs must be an integer from 1 to 600000"}
                return 202, {"id": "task-3", "status": "queued"}
            return 200, {"id": "task-3", "status": "completed", "output": "ok"}

        upstream = FakeUpstream(responder)
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            result = handle({"method": "tools/call", "params": {
                "name": "delegate",
                "arguments": {"agentId": "fake", "input": "work", "requestId": "req-1", "waitMs": 900000}}})
            self.assertNotIn("isError", result)
            task = result["structuredContent"]
            self.assertEqual((task["id"], task["status"]), ("task-3", "completed"))
            self.assertEqual(task["waitMs"], 600000)
            self.assertIs(task["waitClamped"], True)
            self.assertEqual([(call[0], call[1]) for call in upstream.calls],
                             [("POST", "/v1/tasks"), ("GET", "/healthz"), ("POST", "/v1/tasks"),
                              ("GET", "/v1/tasks/task-3?waitMs=600000")])
            first, second = upstream.calls[0][2], upstream.calls[2][2]
            self.assertEqual((first["waitMs"], first["requestId"]), (900000, "req-1"))
            self.assertEqual((second["waitMs"], second["requestId"]), (600000, "req-1"))
        finally:
            upstream.close()

    def test_delegate_returns_the_relay_error_when_it_cannot_clamp_the_wait(self):
        def relay_without_limits(method, path, body):
            if path == "/healthz":
                return 200, {"ok": True}
            return 400, {"error": "invalid_wait", "message": "waitMs must be an integer from 1 to 600000"}

        upstream = FakeUpstream(relay_without_limits)
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            result = handle({"method": "tools/call", "params": {
                "name": "delegate", "arguments": {"agentId": "fake", "input": "work", "waitMs": 900000}}})
            self.assertTrue(result["isError"])
            self.assertEqual(result["structuredContent"]["error"], "invalid_wait")
            self.assertEqual([call[1] for call in upstream.calls], ["/v1/tasks", "/healthz"])
        finally:
            upstream.close()

    def test_delegate_still_waits_when_the_relay_predates_the_waitMs_field(self):
        def old_relay(method, path, body):
            if path == "/healthz":
                return 200, {"ok": True, "limits": {"maxWaitMs": 600000}}
            if method == "POST":
                if "waitMs" in body:
                    return 400, {"error": "unknown_field", "message": "Unknown request field: waitMs"}
                return 202, {"id": "task-4", "status": "queued"}
            return 200, {"id": "task-4", "status": "completed", "output": "ok"}

        upstream = FakeUpstream(old_relay)
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            result = handle({"method": "tools/call", "params": {
                "name": "delegate", "arguments": {"agentId": "fake", "input": "work", "waitMs": 3000}}})
            self.assertNotIn("isError", result)
            self.assertEqual(result["structuredContent"]["status"], "completed")
            self.assertNotIn("waitClamped", result["structuredContent"])
            self.assertEqual([call[1] for call in upstream.calls],
                             ["/v1/tasks", "/healthz", "/v1/tasks", "/v1/tasks/task-4?waitMs=3000"])
            self.assertNotIn("waitMs", upstream.calls[2][2])
        finally:
            upstream.close()

    def test_delegate_clamps_the_wait_when_an_old_relay_has_a_lower_wait_limit(self):
        def old_relay(method, path, body):
            if path == "/healthz":
                return 200, {"ok": True, "limits": {"maxWaitMs": 600000}}
            if method == "POST":
                if "waitMs" in body:
                    return 400, {"error": "unknown_field", "message": "Unknown request field: waitMs"}
                return 202, {"id": "task-5", "status": "queued"}
            if int(path.rsplit("waitMs=", 1)[1]) > 600000:
                return 400, {"error": "invalid_wait", "message": "waitMs must be an integer from 1 to 600000"}
            return 200, {"id": "task-5", "status": "completed", "output": "ok"}

        upstream = FakeUpstream(old_relay)
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            result = handle({"method": "tools/call", "params": {
                "name": "delegate", "arguments": {"agentId": "fake", "input": "work", "waitMs": 900000}}})
            self.assertNotIn("isError", result)
            task = result["structuredContent"]
            self.assertEqual((task["id"], task["status"]), ("task-5", "completed"))
            self.assertEqual(task["waitMs"], 600000)
            self.assertIs(task["waitClamped"], True)
            self.assertEqual([call[1] for call in upstream.calls],
                             ["/v1/tasks", "/healthz", "/v1/tasks", "/v1/tasks/task-5?waitMs=600000"])
            self.assertNotIn("waitMs", upstream.calls[2][2])
        finally:
            upstream.close()

    def test_delegate_keeps_the_wait_when_an_old_relay_reports_no_wait_limit(self):
        for health in ((500, {"error": "boom"}), (200, {"ok": True})):
            def old_relay(method, path, body, health=health):
                if path == "/healthz":
                    return health
                if method == "POST":
                    if "waitMs" in body:
                        return 400, {"error": "unknown_field", "message": "Unknown request field: waitMs"}
                    return 202, {"id": "task-6", "status": "queued"}
                return 200, {"id": "task-6", "status": "completed", "output": "ok"}

            upstream = FakeUpstream(old_relay)
            handle = mcp_server.create_http_handler(upstream.url, timeout=5)
            try:
                result = handle({"method": "tools/call", "params": {
                    "name": "delegate", "arguments": {"agentId": "fake", "input": "work", "waitMs": 900000}}})
                self.assertNotIn("isError", result)
                self.assertEqual(result["structuredContent"]["status"], "completed")
                self.assertNotIn("waitClamped", result["structuredContent"])
                self.assertNotIn("waitMs", result["structuredContent"])
                self.assertEqual([call[1] for call in upstream.calls],
                                 ["/v1/tasks", "/healthz", "/v1/tasks", "/v1/tasks/task-6?waitMs=900000"])
            finally:
                upstream.close()

    def test_delegate_forwards_options_and_validates_their_shape(self):
        upstream = FakeUpstream(lambda method, path, body: (202, {"id": "task-3", "status": "queued"}))
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            handle({"method": "tools/call", "params": {
                "name": "delegate",
                "arguments": {"agentId": "fake", "input": "work", "options": {"effort": "high"}}}})
            self.assertEqual(upstream.calls[0][2],
                             {"agentId": "fake", "input": "work", "options": {"effort": "high"}})
            for bad in ("high", ["effort"], {"effort": 3}, {"effort": ["high"]}, {"": "high"}):
                with self.assertRaises(mcp_server.RpcError, msg=repr(bad)) as raised:
                    handle({"method": "tools/call", "params": {
                        "name": "delegate",
                        "arguments": {"agentId": "fake", "input": "work", "options": bad}}})
                self.assertEqual(raised.exception.code, -32602)
            self.assertEqual(len(upstream.calls), 1)
        finally:
            upstream.close()

    def test_delegate_forwards_read_only_and_requires_a_boolean(self):
        upstream = FakeUpstream(lambda method, path, body: (202, {"id": "task-4", "status": "queued"}))
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            handle({"method": "tools/call", "params": {
                "name": "delegate", "arguments": {"agentId": "fake", "input": "work", "readOnly": True}}})
            self.assertEqual(upstream.calls[0][2], {"agentId": "fake", "input": "work", "readOnly": True})
            for bad in ("true", 1, None):
                with self.assertRaises(mcp_server.RpcError, msg=repr(bad)) as raised:
                    handle({"method": "tools/call", "params": {
                        "name": "delegate",
                        "arguments": {"agentId": "fake", "input": "work", "readOnly": bad}}})
                self.assertEqual(raised.exception.code, -32602)
            self.assertEqual(len(upstream.calls), 1)
        finally:
            upstream.close()

    def test_delegate_sends_options_read_only_and_the_wait_in_one_submission(self):
        def responder(method, path, body):
            if method == "POST":
                return 202, {"id": "task-7", "status": "queued"}
            return 200, {"id": "task-7", "status": "completed", "output": "ok"}

        upstream = FakeUpstream(responder)
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            result = handle({"method": "tools/call", "params": {"name": "delegate", "arguments": {
                "agentId": "fake", "input": "work", "options": {"effort": "high"}, "readOnly": True,
                "waitMs": 3000}}})
            self.assertEqual(result["structuredContent"]["status"], "completed")
            self.assertEqual(upstream.calls[0][2],
                             {"agentId": "fake", "input": "work", "options": {"effort": "high"},
                              "readOnly": True, "waitMs": 3000})
            self.assertIn("waitMs=3000", upstream.calls[1][1])
        finally:
            upstream.close()

    def test_old_relay_fallback_drops_only_the_wait_and_keeps_options_and_read_only(self):
        def relay_before_waitms(method, path, body):
            if path == "/healthz":
                return 200, {"ok": True, "limits": {"maxWaitMs": 600000}}
            if method == "POST":
                if "waitMs" in body:
                    return 400, {"error": "unknown_field", "message": "Unknown request field: waitMs"}
                return 202, {"id": "task-8", "status": "queued"}
            return 200, {"id": "task-8", "status": "completed", "output": "ok"}

        upstream = FakeUpstream(relay_before_waitms)
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            result = handle({"method": "tools/call", "params": {"name": "delegate", "arguments": {
                "agentId": "fake", "input": "work", "options": {"effort": "high"}, "readOnly": True,
                "waitMs": 3000}}})
            self.assertNotIn("isError", result)
            self.assertEqual(result["structuredContent"]["status"], "completed")
            retry = upstream.calls[2][2]
            self.assertNotIn("waitMs", retry)
            self.assertEqual((retry["options"], retry["readOnly"]), ({"effort": "high"}, True))
        finally:
            upstream.close()

    def test_a_relay_that_refuses_read_only_or_options_is_not_retried_without_them(self):
        # A relay that predates a field names the first unknown one, in sorted order, so options and
        # readOnly come before waitMs. The error must reach the caller: dropping the field would run
        # a task that was meant to be read-only, or to use other options, without them.
        for field, value in (("readOnly", True), ("options", {"effort": "high"})):
            def old_relay(method, path, body):
                unknown = sorted(set(body) - {"agentId", "input", "waitMs"})
                if unknown:
                    return 400, {"error": "unknown_field", "message": f"Unknown request field: {unknown[0]}"}
                return 202, {"id": "task-9", "status": "queued"}

            upstream = FakeUpstream(old_relay)
            handle = mcp_server.create_http_handler(upstream.url, timeout=5)
            try:
                result = handle({"method": "tools/call", "params": {"name": "delegate", "arguments": {
                    "agentId": "fake", "input": "work", field: value, "waitMs": 3000}}})
                self.assertTrue(result["isError"], field)
                self.assertEqual(result["structuredContent"]["error"], "unknown_field", field)
                self.assertIn(field, result["structuredContent"]["message"])
                self.assertEqual([call[1] for call in upstream.calls], ["/v1/tasks"], field)
            finally:
                upstream.close()

    def test_delegate_and_list_agents_describe_read_only(self):
        handle = mcp_server.create_http_handler("http://127.0.0.1:1", timeout=1)
        tools = {tool["name"]: tool for tool in handle({"method": "tools/list"})["tools"]}
        read_only = tools["delegate"]["inputSchema"]["properties"]["readOnly"]
        self.assertEqual(read_only["type"], "boolean")
        self.assertIn("list_agents", read_only["description"])
        self.assertIn("readOnly", tools["list_agents"]["description"])

    def test_delegate_and_list_agents_describe_options(self):
        handle = mcp_server.create_http_handler("http://127.0.0.1:1", timeout=1)
        tools = {tool["name"]: tool for tool in handle({"method": "tools/list"})["tools"]}
        options = tools["delegate"]["inputSchema"]["properties"]["options"]
        self.assertEqual(options["type"], "object")
        self.assertEqual(options["additionalProperties"], {"type": "string"})
        self.assertIn("list_agents", options["description"])
        self.assertIn("options", tools["list_agents"]["description"])

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

    def test_maps_workflow_tools_onto_the_http_workflow_endpoints(self):
        def responder(method, path, body):
            route = path.split("?")[0]
            if route == "/v1/workflows":
                if method == "POST":
                    return 202, {"id": "wf-1", "status": "queued"}
                return 200, {"workflows": [{"id": "wf-1", "status": "completed"}]}
            if route == "/v1/workflows/wf-1/resume":
                return 202, {"id": "wf-1", "status": "queued"}
            if method == "DELETE":
                return 200, {"id": "wf-1", "status": "cancelled"}
            return 200, {"id": "wf-1", "status": "completed", "steps": []}

        upstream = FakeUpstream(responder)
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        spec = {"steps": [{"id": "a", "agentId": "fake", "input": "x"}]}
        try:
            submitted = handle({"method": "tools/call", "params": {
                "name": "run_workflow", "arguments": {"spec": spec, "waitMs": 2000}}})
            self.assertEqual(submitted["structuredContent"]["status"], "completed")
            self.assertEqual(upstream.calls[0][:2], ("POST", "/v1/workflows"))
            self.assertEqual(upstream.calls[0][2], spec)
            self.assertEqual(upstream.calls[1][1], "/v1/workflows/wf-1?waitMs=2000")

            fetched = handle({"method": "tools/call", "params": {
                "name": "get_workflow",
                "arguments": {"workflowId": "wf-1", "maxWaitMs": 500}}})
            self.assertEqual(fetched["structuredContent"]["status"], "completed")
            self.assertEqual(upstream.calls[2][1], "/v1/workflows/wf-1?waitMs=500")

            listed = handle({"method": "tools/call", "params": {
                "name": "list_workflows", "arguments": {"status": "completed", "limit": 5}}})
            self.assertEqual(len(listed["structuredContent"]["workflows"]), 1)
            self.assertEqual(upstream.calls[3][1], "/v1/workflows?status=completed&limit=5")

            cancelled = handle({"method": "tools/call", "params": {
                "name": "cancel_workflow", "arguments": {"workflowId": "wf-1"}}})
            self.assertEqual(cancelled["structuredContent"]["status"], "cancelled")
            self.assertEqual(upstream.calls[4][0], "DELETE")

            resumed = handle({"method": "tools/call", "params": {
                "name": "resume_workflow",
                "arguments": {"workflowId": "wf-1", "waitMs": 1000}}})
            self.assertEqual(resumed["structuredContent"]["status"], "completed")
            self.assertEqual(upstream.calls[5][:2], ("POST", "/v1/workflows/wf-1/resume"))
            self.assertEqual(upstream.calls[6][1], "/v1/workflows/wf-1?waitMs=1000")
            self.assertEqual(len(upstream.calls), 7)
        finally:
            upstream.close()

    def test_maps_get_workflow_view_and_step_selectors_onto_the_relay(self):
        def responder(method, path, body):
            return 200, {"id": "wf-1", "status": "completed",
                         "steps": [{"id": "a", "outputChars": 4, "firstLine": "beta"}]}

        upstream = FakeUpstream(responder)
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            handle({"method": "tools/call", "params": {
                "name": "get_workflow",
                "arguments": {"workflowId": "wf-1", "view": "summary", "step": "a"}}})
            self.assertEqual(upstream.calls[0][1], "/v1/workflows/wf-1?view=summary&step=a")
            self.assertEqual(upstream.calls[0][2], None)

            # Both selectors ride along on the long-poll path too.
            handle({"method": "tools/call", "params": {
                "name": "get_workflow",
                "arguments": {"workflowId": "wf-1", "view": "summary", "maxWaitMs": 500}}})
            self.assertEqual(upstream.calls[1][1], "/v1/workflows/wf-1?waitMs=500&view=summary")

            for arguments in ({"workflowId": "wf-1", "view": "full"},
                              {"workflowId": "wf-1", "step": "not a step"},
                              {"workflowId": "wf-1", "step": ""}):
                with self.subTest(arguments=arguments):
                    with self.assertRaises(mcp_server.RpcError) as raised:
                        handle({"method": "tools/call",
                                "params": {"name": "get_workflow", "arguments": arguments}})
                    self.assertEqual(raised.exception.code, -32602)
            self.assertEqual(len(upstream.calls), 2)
        finally:
            upstream.close()

    def test_maps_wait_workflow_events_onto_the_events_endpoint(self):
        def responder(method, path, body):
            if path.startswith("/v1/workflows/events"):
                return 200, {"events": [{"seq": 7, "workflowId": "wf-1", "stepId": "a",
                                         "oldStatus": "running", "newStatus": "completed"}]}
            return 200, {}

        upstream = FakeUpstream(responder)
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            result = handle({"method": "tools/call", "params": {
                "name": "wait_workflow_events",
                "arguments": {"sessionId": "s-1", "after": 6, "maxWaitMs": 5000}}})
            events = result["structuredContent"]["events"]
            self.assertEqual(events[0]["seq"], 7)
            self.assertEqual(events[0]["newStatus"], "completed")
            self.assertEqual(upstream.calls[0][0], "GET")
            self.assertEqual(upstream.calls[0][1],
                             "/v1/workflows/events?sessionId=s-1&after=6&waitMs=5000")

            # after defaults to 0 and maxWaitMs to 60000.
            handle({"method": "tools/call", "params": {
                "name": "wait_workflow_events", "arguments": {"sessionId": "s-1"}}})
            self.assertEqual(upstream.calls[1][1],
                             "/v1/workflows/events?sessionId=s-1&after=0&waitMs=60000")
        finally:
            upstream.close()

    def test_wait_workflow_events_clamps_a_wait_above_the_relay_limit(self):
        def responder(method, path, body):
            if path == "/healthz":
                return 200, {"ok": True, "limits": {"maxWaitMs": 600000}}
            if "waitMs=900000" in path:
                return 400, {"error": "invalid_wait",
                             "message": "waitMs must be an integer from 1 to 600000"}
            return 200, {"events": []}

        upstream = FakeUpstream(responder)
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            result = handle({"method": "tools/call", "params": {
                "name": "wait_workflow_events",
                "arguments": {"sessionId": "s-1", "maxWaitMs": 900000}}})
            self.assertNotIn("isError", result)
            value = result["structuredContent"]
            self.assertEqual(value["events"], [])
            self.assertEqual(value["waitMs"], 600000)
            self.assertIs(value["waitClamped"], True)
            self.assertEqual([call[1] for call in upstream.calls],
                             ["/v1/workflows/events?sessionId=s-1&after=0&waitMs=900000",
                              "/healthz",
                              "/v1/workflows/events?sessionId=s-1&after=0&waitMs=600000"])
        finally:
            upstream.close()

    def test_validates_workflow_tool_arguments_before_calling_the_relay(self):
        upstream = FakeUpstream(lambda method, path, body: (200, {}))
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        cases = [
            ("run_workflow", {"spec": "not an object"}),
            ("run_workflow", {"spec": {"steps": []}, "waitMs": 0}),
            ("get_workflow", {"workflowId": "wf-1", "maxWaitMs": 0}),
            ("get_workflow", {"workflowId": ""}),
            ("list_workflows", {"status": "timed_out"}),
            ("cancel_workflow", {}),
            ("resume_workflow", {"workflowId": "wf-1", "waitMs": -1}),
            ("wait_workflow_events", {}),
            ("wait_workflow_events", {"sessionId": "s-1", "after": -1}),
            ("wait_workflow_events", {"sessionId": "s-1", "after": 1.5}),
            ("wait_workflow_events", {"sessionId": "s-1", "maxWaitMs": 0}),
        ]
        try:
            for name, arguments in cases:
                with self.subTest(name=name, arguments=arguments):
                    with self.assertRaises(mcp_server.RpcError) as raised:
                        handle({"method": "tools/call",
                                "params": {"name": name, "arguments": arguments}})
                    self.assertEqual(raised.exception.code, -32602)
            self.assertEqual(len(upstream.calls), 0)
        finally:
            upstream.close()

    def test_run_workflow_reads_a_spec_file_and_submits_the_document_unchanged(self):
        def responder(method, path, body):
            if method == "POST" and path == "/v1/workflows":
                return 202, {"id": "wf-1", "status": "queued"}
            return 200, {"id": "wf-1", "status": "completed", "steps": []}

        upstream = FakeUpstream(responder)
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        document = {"name": "review-loop",
                    "steps": [{"id": "a", "agentId": "fake", "input": "x"},
                              {"id": "b", "agentId": "fake", "needs": ["a"], "input": "y"}]}
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "workflow.json")
                with open(path, "w", encoding="utf-8") as saved:
                    json.dump(document, saved)
                result = handle({"method": "tools/call", "params": {
                    "name": "run_workflow", "arguments": {"specPath": path, "waitMs": 2000}}})
            self.assertNotIn("isError", result)
            self.assertEqual(result["structuredContent"]["status"], "completed")
            self.assertEqual(upstream.calls[0][:2], ("POST", "/v1/workflows"))
            self.assertEqual(upstream.calls[0][2], document)
            self.assertEqual(upstream.calls[1][1], "/v1/workflows/wf-1?waitMs=2000")
            tools = {tool["name"]: tool for tool in handle({"method": "tools/list"})["tools"]}
            schema = tools["run_workflow"]["inputSchema"]
            spec_path = schema["properties"]["specPath"]
            self.assertEqual(spec_path["type"], "string")
            self.assertTrue(spec_path["description"])
            self.assertEqual(schema["anyOf"], [{"required": ["spec"]}, {"required": ["specPath"]}])
            self.assertNotIn("required", schema)
        finally:
            upstream.close()

    def test_run_workflow_refuses_a_bad_spec_path_before_it_calls_the_relay(self):
        upstream = FakeUpstream(lambda method, path, body: (202, {"id": "wf-1", "status": "queued"}))
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            with tempfile.TemporaryDirectory() as directory:
                saved_path = os.path.join(directory, "workflow.json")
                with open(saved_path, "w", encoding="utf-8") as saved:
                    json.dump({"steps": []}, saved)
                broken_path = os.path.join(directory, "broken.json")
                with open(broken_path, "w", encoding="utf-8") as broken:
                    broken.write("{not json")
                cases = [
                    ({"spec": {"steps": []}, "specPath": saved_path},
                     "exactly one of spec or specPath"),
                    ({"specPath": os.path.join(directory, "absent.json")}, "cannot be read"),
                    ({"specPath": broken_path}, "not valid JSON"),
                    ({"specPath": "workflow.json"}, "absolute path"),
                    ({}, "exactly one of spec or specPath"),
                ]
                for arguments, needle in cases:
                    with self.subTest(arguments=arguments):
                        with self.assertRaises(mcp_server.RpcError) as raised:
                            handle({"method": "tools/call", "params": {
                                "name": "run_workflow", "arguments": arguments}})
                        self.assertEqual(raised.exception.code, -32602)
                        self.assertIn(needle, raised.exception.message)
            self.assertEqual(upstream.calls, [])
        finally:
            upstream.close()

    def test_run_workflow_refuses_a_spec_file_that_is_not_utf8(self):
        upstream = FakeUpstream(lambda method, path, body: (202, {"id": "wf-1", "status": "queued"}))
        handle = mcp_server.create_http_handler(upstream.url, timeout=5)
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "workflow.json")
                for payload in (b"\xff\xfe{", b'{"steps": []}\x80'):
                    with open(path, "wb") as saved:
                        saved.write(payload)
                    with self.subTest(payload=payload):
                        with self.assertRaises(mcp_server.RpcError) as raised:
                            handle({"method": "tools/call", "params": {
                                "name": "run_workflow", "arguments": {"specPath": path}}})
                        self.assertEqual(raised.exception.code, -32602)
                        self.assertIn(path, raised.exception.message)
            self.assertEqual(upstream.calls, [])
        finally:
            upstream.close()

    def test_run_workflow_spec_schema_documents_the_step_fields(self):
        handle = mcp_server.create_http_handler("http://127.0.0.1:1", timeout=1)
        tools = {tool["name"]: tool for tool in handle({"method": "tools/list"})["tools"]}
        spec = tools["run_workflow"]["inputSchema"]["properties"]["spec"]
        self.assertEqual(spec["required"], ["steps"])
        step = spec["properties"]["steps"]["items"]
        self.assertEqual(step["required"], ["id", "agentId", "input"])
        for key in ("needs", "foreach", "retries", "repeat", "timeoutMs", "cwd",
                    "failIfOutputMatches"):
            self.assertTrue(step["properties"][key].get("description"), f"{key} has no description")
        self.assertEqual(step["properties"]["failIfOutputMatches"]["type"], "string")
        self.assertIn("list_agents", step["properties"]["agentId"]["description"])
        self.assertIn("{{id.output}}", step["properties"]["id"]["description"])

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
