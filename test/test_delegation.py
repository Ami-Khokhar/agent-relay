"""Recursive delegation policy: lineage, allowed targets, cycles, depth, and task budgets."""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from relay_helpers import PYTHON, Relay, send_json, start_http_server, stop_http_server
import mcp_server

SLEEP = ["-c", "import time; time.sleep(10)"]


def agent(agent_id, **extra):
    return {"id": agent_id, "command": PYTHON, "args": SLEEP, **extra}


class DelegationPolicyTests(unittest.TestCase):
    def start(self, agents, env=None):
        relay = Relay(agents, env=env)
        self.addCleanup(relay.close)
        return relay

    def delegate(self, relay, parent, agent_id):
        return relay.submit(agentId=agent_id, input="sub-task", parentTaskId=parent["id"])

    def test_spawned_agents_receive_their_task_id_for_lineage(self):
        relay = self.start({"id": "echo", "command": PYTHON,
                            "args": ["-c", "import os; print(os.environ['AGENT_RELAY_PARENT_TASK_ID'])"]})
        _, submitted = relay.submit(agentId="echo", input="x")
        self.assertEqual(relay.wait_task(submitted["id"])["output"], submitted["id"])
        self.assertEqual(submitted["depth"], 0)
        self.assertNotIn("parentTaskId", submitted)

    def test_rejects_targets_outside_the_parent_agents_delegate_to_list(self):
        relay = self.start([agent("lead", delegateTo=["helper"]), agent("helper"), agent("other")])
        _, root = relay.submit(agentId="lead", input="plan")
        status, payload = self.delegate(relay, root, "other")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "delegation_not_allowed")
        status, child = self.delegate(relay, root, "helper")
        self.assertEqual(status, 202)
        self.assertEqual((child["parentTaskId"], child["depth"]), (root["id"], 1))

    def test_rejects_cyclic_delegation(self):
        relay = self.start([agent("a"), agent("b")])
        _, root = relay.submit(agentId="a", input="x")
        status, payload = self.delegate(relay, root, "a")
        self.assertEqual((status, payload["error"]), (409, "delegation_cycle"))
        _, child = self.delegate(relay, root, "b")
        status, payload = self.delegate(relay, child, "a")
        self.assertEqual((status, payload["error"]), (409, "delegation_cycle"))
        self.assertIn("a > b", payload["message"])

    def test_rejects_delegation_beyond_the_depth_limit(self):
        relay = self.start([agent("a"), agent("b"), agent("c")],
                           env={"AGENT_RELAY_MAX_DELEGATION_DEPTH": "1"})
        _, root = relay.submit(agentId="a", input="x")
        status, child = self.delegate(relay, root, "b")
        self.assertEqual(status, 202)
        status, payload = self.delegate(relay, child, "c")
        self.assertEqual((status, payload["error"]), (409, "delegation_depth_exceeded"))

        disabled = self.start([agent("a"), agent("b")], env={"AGENT_RELAY_MAX_DELEGATION_DEPTH": "0"})
        _, root = disabled.submit(agentId="a", input="x")
        status, payload = self.delegate(disabled, root, "b")
        self.assertEqual((status, payload["error"]), (409, "delegation_depth_exceeded"))

    def test_rejects_delegation_once_the_task_tree_budget_is_spent(self):
        relay = self.start([agent("a"), agent("b"), agent("c")],
                           env={"AGENT_RELAY_MAX_DELEGATED_TASKS": "2"})
        _, root = relay.submit(agentId="a", input="x")
        _, child = self.delegate(relay, root, "b")
        self.assertEqual(self.delegate(relay, child, "c")[0], 202)
        status, payload = self.delegate(relay, root, "c")
        self.assertEqual((status, payload["error"]), (429, "delegation_budget_exhausted"))
        # A new root task has its own budget.
        _, other_root = relay.submit(agentId="a", input="y")
        self.assertEqual(self.delegate(relay, other_root, "b")[0], 202)

    def test_rejects_an_unknown_parent_task(self):
        relay = self.start(agent("a"))
        status, payload = relay.submit(agentId="a", input="x", parentTaskId="no-such-task")
        self.assertEqual((status, payload["error"]), (400, "unknown_parent_task"))


class McpLineageTests(unittest.TestCase):
    def test_delegate_forwards_the_parent_task_id_set_by_the_relay(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.rfile.read(int(request.headers["content-length"]))))
            send_json(request, 202, {"id": "child", "status": "queued"})

        server, port = start_http_server(handler)
        try:
            handle = mcp_server.create_http_handler(f"http://127.0.0.1:{port}", timeout=5)
            call = {"method": "tools/call",
                    "params": {"name": "delegate", "arguments": {"agentId": "b", "input": "x"}}}
            with mock.patch.dict(os.environ, {"AGENT_RELAY_PARENT_TASK_ID": "parent-1"}):
                handle(call)
            with mock.patch.dict(os.environ, {"AGENT_RELAY_PARENT_TASK_ID": ""}):
                handle(call)
            self.assertEqual(bodies[0]["parentTaskId"], "parent-1")
            self.assertNotIn("parentTaskId", bodies[1])
        finally:
            stop_http_server(server)


if __name__ == "__main__":
    unittest.main()
