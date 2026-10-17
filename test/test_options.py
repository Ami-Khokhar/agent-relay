"""Per-task agent options: owner-declared named options mapped to fixed args."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from relay_helpers import PRINT_ARGV, PYTHON, STDIO_ARGV, McpClient, Relay
import server

EFFORT = {"low": ["-c", "model_reasoning_effort=low"], "high": ["-c", "model_reasoning_effort=high"]}


def fake(**extra):
    return {"id": "fake", "command": PYTHON, "args": [*PRINT_ARGV, "--base"], **extra}


class OptionsTests(unittest.TestCase):
    def start(self, agent):
        relay = Relay(agent)
        self.addCleanup(relay.close)
        return relay

    def argv_of(self, relay, **body):
        task = relay.run_task(agentId="fake", **body)
        self.assertEqual(task["status"], "completed", task)
        return json.loads(task["output"])

    def test_runs_the_agent_with_the_mapped_args_between_its_args_and_the_input(self):
        relay = self.start(fake(options={"effort": EFFORT}))
        status, submitted = relay.submit(agentId="fake", input="review it", options={"effort": "high"})
        self.assertEqual(status, 202)
        self.assertEqual(submitted["options"], {"effort": "high"})
        task = relay.wait_task(submitted["id"])
        self.assertEqual(json.loads(task["output"]),
                         ["--base", "-c", "model_reasoning_effort=high", "review it"])

    def test_orders_several_options_by_the_registry_not_by_the_caller(self):
        relay = self.start(fake(options={"effort": EFFORT, "mode": {"fast": ["--fast"]}}))
        argv = self.argv_of(relay, input="go", options={"mode": "fast", "effort": "low"})
        self.assertEqual(argv, ["--base", "-c", "model_reasoning_effort=low", "--fast", "go"])

    def test_a_task_without_options_runs_exactly_as_before(self):
        relay = self.start(fake(options={"effort": EFFORT}))
        for body in ({}, {"options": {}}):
            _, submitted = relay.submit(agentId="fake", input="plain", **body)
            self.assertNotIn("options", submitted)
            task = relay.wait_task(submitted["id"])
            self.assertEqual(json.loads(task["output"]), ["--base", "plain"])

    def test_an_option_may_map_to_no_args(self):
        relay = self.start(fake(options={"effort": {"default": [], "high": ["--high"]}}))
        self.assertEqual(self.argv_of(relay, input="x", options={"effort": "default"}), ["--base", "x"])

    def test_passes_the_mapped_args_to_a_stdio_adapter_too(self):
        relay = self.start({"id": "fake", "type": "stdio", "command": PYTHON,
                            "args": ["-c", STDIO_ARGV, "--base"], "options": {"effort": EFFORT}})
        argv = self.argv_of(relay, input="x", options={"effort": "high"})
        self.assertEqual(argv, ["--base", "-c", "model_reasoning_effort=high"])

    def test_refuses_an_unknown_option_name_and_runs_nothing(self):
        relay = self.start(fake(options={"effort": EFFORT}))
        status, payload = relay.submit(agentId="fake", input="x", options={"model": "big"})
        self.assertEqual((status, payload["error"]), (400, "unknown_option"))
        self.assertIn("model", payload["message"])
        self.assertIn("effort", payload["message"])
        self.assertEqual(relay.request("GET", "/v1/tasks")[1]["tasks"], [])

    def test_refuses_an_option_value_the_owner_did_not_allow(self):
        relay = self.start(fake(options={"effort": EFFORT}))
        for value in ("extreme", "--sandbox=none", "-c", " high", "HIGH"):
            status, payload = relay.submit(agentId="fake", input="x", options={"effort": value})
            self.assertEqual((status, payload["error"]), (400, "invalid_option_value"), value)
            self.assertIn("high", payload["message"])
        self.assertEqual(relay.request("GET", "/v1/tasks")[1]["tasks"], [])

    def test_refuses_options_for_an_agent_that_declares_none(self):
        relay = self.start(fake())
        status, payload = relay.submit(agentId="fake", input="x", options={"effort": "high"})
        self.assertEqual((status, payload["error"]), (400, "unknown_option"))
        self.assertIn("declares no options", payload["message"])

    def test_accepts_options_read_only_and_waitMs_together_and_refuses_before_a_task_exists(self):
        relay = self.start(fake(options={"effort": EFFORT}, readOnly={"args": ["--ro"]}))
        status, submitted = relay.submit(agentId="fake", input="go", options={"effort": "high"},
                                         readOnly=True, waitMs=1000)
        self.assertEqual(status, 202, submitted)
        self.assertEqual((submitted["options"], submitted["readOnly"]), ({"effort": "high"}, True))
        self.assertNotIn("waitMs", submitted)  # validate-only: never stored on the task
        task = relay.wait_task(submitted["id"])
        self.assertEqual(task["status"], "completed", task)
        stored = relay.request("GET", "/v1/tasks")[1]["tasks"]
        self.assertEqual(len(stored), 1)
        refused = [
            ({"options": {"effort": "high"}, "waitMs": 0}, "invalid_wait"),
            ({"readOnly": True, "waitMs": "soon"}, "invalid_wait"),
            ({"options": {"effort": "extreme"}, "waitMs": 1000}, "invalid_option_value"),
            ({"options": {"model": "big"}, "waitMs": 1000}, "unknown_option"),
            ({"readOnly": "yes", "waitMs": 1000}, "invalid_read_only"),
        ]
        for body, code in refused:
            status, payload = relay.submit(agentId="fake", input="no", **body)
            self.assertEqual((status, payload["error"]), (400, code), body)
        self.assertEqual(len(relay.request("GET", "/v1/tasks")[1]["tasks"]), 1, "a refused task was created")
        # An agent without a profile still refuses a read-only task whose wait is valid.
        bare = self.start(fake())
        status, payload = bare.submit(agentId="fake", input="no", readOnly=True, waitMs=1000)
        self.assertEqual((status, payload["error"]), (400, "read_only_unsupported"))
        self.assertEqual(bare.request("GET", "/v1/tasks")[1]["tasks"], [])

    def test_refuses_options_that_are_not_an_object_of_strings(self):
        relay = self.start(fake(options={"effort": EFFORT}))
        for bad in ("high", ["effort"], 5, True, {"effort": 3}, {"effort": ["high"]},
                    {"effort": {"high": 1}}, {"": "high"}, {"effort": None}):
            status, payload = relay.submit(agentId="fake", input="x", options=bad)
            self.assertEqual((status, payload["error"]), (400, "invalid_options"), repr(bad))
        self.assertEqual(relay.request("GET", "/v1/tasks")[1]["tasks"], [])

    def test_a_retry_with_different_options_is_a_conflict(self):
        relay = self.start(fake(options={"effort": EFFORT, "mode": {"fast": ["--fast"]}}))
        first = relay.submit(agentId="fake", input="x", requestId="r1",
                             options={"effort": "high", "mode": "fast"})
        self.assertEqual(first[0], 202)
        same = relay.submit(agentId="fake", input="x", requestId="r1",
                            options={"mode": "fast", "effort": "high"})
        self.assertEqual((same[0], same[1]["id"]), (200, first[1]["id"]))
        for options in ({"effort": "low", "mode": "fast"}, {"effort": "high"}, None):
            body = {} if options is None else {"options": options}
            status, payload = relay.submit(agentId="fake", input="x", requestId="r1", **body)
            self.assertEqual((status, payload["error"]), (409, "idempotency_conflict"), options)

    def test_lists_each_agents_option_names_and_allowed_values(self):
        relay = self.start([fake(options={"effort": EFFORT, "mode": {"fast": ["--fast"]}}),
                            {"id": "plain", "command": PYTHON, "args": PRINT_ARGV}])
        listing = {entry["id"]: entry for entry in relay.request("GET", "/v1/agents")[1]["agents"]}
        self.assertEqual(listing["fake"]["options"], {"effort": ["low", "high"], "mode": ["fast"]})
        self.assertNotIn("options", listing["plain"])

    def test_the_mcp_delegate_tool_passes_options_to_the_relay(self):
        relay = self.start(fake(options={"effort": EFFORT}))
        client = McpClient(relay.base)
        self.addCleanup(client.close)
        task = client.call_tool("delegate", {"agentId": "fake", "input": "review it",
                                             "options": {"effort": "high"}, "waitMs": 5000})
        self.assertFalse(task.get("isError"), task)
        done = task["structuredContent"]
        self.assertEqual(done["options"], {"effort": "high"})
        self.assertEqual(json.loads(done["output"]),
                         ["--base", "-c", "model_reasoning_effort=high", "review it"])
        refused = client.call_tool("delegate", {"agentId": "fake", "input": "x",
                                                "options": {"effort": "extreme"}})
        self.assertTrue(refused["isError"])
        self.assertEqual(refused["structuredContent"]["error"], "invalid_option_value")


class OptionsRegistryTests(unittest.TestCase):
    def load(self, agent):
        directory = tempfile.mkdtemp(prefix="a2a-options-")
        self.addCleanup(shutil.rmtree, directory, True)
        config = os.path.join(directory, "agents.json")
        with open(config, "w", encoding="utf-8") as handle:
            json.dump({"agents": [agent]}, handle)
        return server.load_registry(config)

    def test_accepts_a_valid_options_declaration(self):
        agents = self.load(fake(options={"effort": EFFORT, "mode": {"x": []}}))
        self.assertEqual(agents["fake"]["options"]["effort"]["high"], ["-c", "model_reasoning_effort=high"])

    def test_rejects_a_malformed_options_declaration_with_a_clear_error(self):
        cases = [
            ("options must be an object", ["effort"]),
            ("option effort must be an object", {"effort": ["low"]}),
            ("option effort must declare at least one value", {"effort": {}}),
            ("option names must be non-empty strings", {"": {"low": []}}),
            ("option effort has an empty value name", {"effort": {"": []}}),
            ("option effort value low must be an array of strings", {"effort": {"low": "-c"}}),
            ("option effort value low must be an array of strings", {"effort": {"low": ["-c", 1]}}),
        ]
        for message, options in cases:
            with self.subTest(options=options):
                with self.assertRaisesRegex(ValueError, f"agent fake: {message}"):
                    self.load(fake(options=options))

    def test_rejects_options_on_an_http_agent(self):
        with self.assertRaisesRegex(ValueError, "agent remote: options need a command or stdio agent"):
            self.load({"id": "remote", "type": "http", "url": "http://127.0.0.1:9000/run",
                       "options": {"effort": EFFORT}})


if __name__ == "__main__":
    unittest.main()
