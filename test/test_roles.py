"""Roles: a worker carries a role, and the role carries the instructions it is run with."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from relay_helpers import PRINT_ARGV, PYTHON, send_json, start_http_server, stop_http_server, Relay
import server

# A stdio adapter that answers with the relay.adapter/v1 document it received, so a test can read
# exactly what reached the adapter.
STDIO_ECHO = (
    "import json, sys\n"
    "doc = json.load(sys.stdin)\n"
    "print(json.dumps({'protocolVersion': 'relay.adapter/v1', 'status': 'completed',\n"
    "                  'output': json.dumps(doc)}))\n"
)

REVIEWER = "You review the change against the intent that was stated.\nReport defects only."
IMPLEMENTER = "You implement the change and prove it."
ROLES = {
    "reviewer": {"description": "Reviews a diff; never edits.",
                 "prompt": REVIEWER, "inputHeader": "Review this work:"},
    "implementer": {"description": "Implements the change.",
                    "prompt": IMPLEMENTER},
}
EXAMPLE_REGISTRY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "agents.example.json")


def fake(**extra):
    """A command agent that prints the arguments it received."""
    return {"id": "fake", "command": PYTHON, "args": [*PRINT_ARGV], **extra}


def registry(agents, roles=ROLES, **top):
    return {"agents": agents if isinstance(agents, list) else [agents], "roles": roles, **top}


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


class RolesTests(unittest.TestCase):
    def start(self, agents, roles=ROLES):
        relay = Relay(registry(agents, roles))
        self.addCleanup(relay.close)
        return relay

    def argv_of(self, relay, **body):
        task = relay.run_task(agentId="fake", **body)
        self.assertEqual(task["status"], "completed", task)
        return json.loads(task["output"])

    def test_a_command_worker_receives_its_role_instructions_then_the_input(self):
        relay = self.start(fake(role="reviewer"))
        argv = self.argv_of(relay, input="check the diff")
        self.assertEqual(argv, [f"{REVIEWER}\n\nReview this work:\n\ncheck the diff"])

    def test_the_task_names_the_role_and_the_instructions_digest_but_not_the_prompt(self):
        relay = self.start(fake(role="reviewer"))
        task = relay.run_task(agentId="fake", input="check the diff")
        self.assertEqual(task["role"], "reviewer")
        self.assertEqual(task["rolePromptSha256"], sha(REVIEWER))
        self.assertEqual(task["inputChars"], len("check the diff"))
        self.assertNotIn(REVIEWER, json.dumps(task))

    def test_a_worker_without_a_role_runs_exactly_as_before(self):
        relay = self.start(fake())
        task = relay.run_task(agentId="fake", input="plain")
        self.assertEqual(json.loads(task["output"]), ["plain"])
        self.assertNotIn("role", task)
        self.assertNotIn("rolePromptSha256", task)

    def test_the_listing_shows_the_role_so_a_caller_can_choose_by_role(self):
        relay = self.start([fake(role="reviewer"), {"id": "other", "command": PYTHON,
                                                    "args": PRINT_ARGV}])
        _, payload = relay.request("GET", "/v1/agents")
        by_id = {entry["id"]: entry for entry in payload["agents"]}
        self.assertEqual(by_id["fake"]["role"], "reviewer")
        self.assertEqual(by_id["fake"]["roleDescription"], "Reviews a diff; never edits.")
        self.assertNotIn("role", by_id["other"])
        self.assertNotIn(REVIEWER, json.dumps(payload))

    def test_healthz_reports_the_role_prompt_limit(self):
        relay = self.start(fake(role="reviewer"))
        _, payload = relay.request("GET", "/healthz")
        self.assertEqual(payload["limits"]["maxRolePromptBytes"], server.MAX_ROLE_PROMPT)

    def test_an_input_that_no_longer_fits_with_the_instructions_is_refused_before_it_runs(self):
        relay = self.start(fake(role="reviewer"))
        room = server.MAX_COMMAND_INPUT - len(f"{REVIEWER}\n\nReview this work:\n\n".encode())
        status, payload = relay.submit(agentId="fake", input="x" * (room + 1))
        self.assertEqual((status, payload["error"]), (413, "role_input_too_large"))
        self.assertIn("AGENT_RELAY_MAX_COMMAND_INPUT_BYTES", payload["message"])
        self.assertEqual(relay.request("GET", "/v1/tasks")[1]["tasks"], [])

    def test_an_input_within_the_budget_still_runs(self):
        relay = self.start(fake(role="reviewer"))
        room = server.MAX_COMMAND_INPUT - len(f"{REVIEWER}\n\nReview this work:\n\n".encode())
        status, submitted = relay.submit(agentId="fake", input="y" * room)
        self.assertEqual(status, 202, submitted)
        self.assertEqual(relay.wait_task(submitted["id"])["status"], "completed")

    def test_prompt_file_is_read_from_the_registry_directory(self):
        relay = Relay(registry([fake(role="reviewer")],
                               {"reviewer": {"description": "Reviews.", "promptFile": "r.md"}}),
                      files={"r.md": "from the file\n"})
        self.addCleanup(relay.close)
        self.assertEqual(self.argv_of(relay, input="x"), ["from the file\n\nx"])

    def test_reload_replaces_the_instructions_of_the_next_task(self):
        relay = self.start(fake(role="reviewer"))
        self.assertEqual(self.argv_of(relay, input="x")[0], f"{REVIEWER}\n\nReview this work:\n\nx")
        relay.write_registry(registry([fake(role="reviewer")],
                                      {"reviewer": {"description": "Reviews.", "prompt": "new order"}}))
        relay.reload()
        task = relay.run_task(agentId="fake", input="x")
        self.assertEqual(json.loads(task["output"]), ["new order\n\nx"])
        self.assertEqual(task["rolePromptSha256"], sha("new order"))

    def test_an_adapter_delivery_role_travels_as_its_own_field(self):
        captured = {}

        def handler(request):
            captured.update(json.loads(request.rfile.read(int(request.headers["content-length"]))))
            send_json(request, 200, {"protocolVersion": "relay.adapter/v1", "status": "completed",
                                     "output": "done"})

        http_server, port = start_http_server(handler)
        self.addCleanup(stop_http_server, http_server)
        relay = self.start(fake(role="reviewer"))
        relay.write_registry(registry(
            [{"id": "fake", "type": "http", "url": f"http://127.0.0.1:{port}/run",
              "role": "reviewer"}],
            {"reviewer": {"description": "Reviews.", "prompt": REVIEWER, "delivery": "adapter"}}))
        relay.reload()
        self.assertEqual(relay.run_task(agentId="fake", input="the input")["status"], "completed")
        self.assertEqual(captured["role"], {"id": "reviewer", "instructions": REVIEWER})
        self.assertEqual(captured["task"]["input"], "the input")

    def test_a_stdio_worker_with_prepend_delivery_gets_the_instructions_in_its_input(self):
        relay = self.start({"id": "fake", "type": "stdio", "command": PYTHON,
                            "args": ["-c", STDIO_ECHO], "role": "implementer"})
        document = json.loads(relay.run_task(agentId="fake", input="build it")["output"])
        self.assertEqual(document["task"]["input"], f"{IMPLEMENTER}\n\nbuild it")
        self.assertNotIn("role", document)

    def test_an_http_worker_without_a_role_gets_the_document_it_always_got(self):
        captured = {}

        def handler(request):
            captured.update(json.loads(request.rfile.read(int(request.headers["content-length"]))))
            send_json(request, 200, {"protocolVersion": "relay.adapter/v1", "status": "completed"})

        http_server, port = start_http_server(handler)
        self.addCleanup(stop_http_server, http_server)
        relay = self.start({"id": "fake", "type": "http", "url": f"http://127.0.0.1:{port}/run"})
        relay.run_task(agentId="fake", input="plain")
        self.assertNotIn("role", captured)
        self.assertEqual(set(captured), {"protocolVersion", "task"})


class RoleRegistryTests(unittest.TestCase):
    """Load-time validation: a role problem is a registry error that keeps the running registry."""

    def load(self, config, files=None):
        directory = tempfile.mkdtemp(prefix="agent-relay-roles-")
        self.addCleanup(shutil.rmtree, directory, True)
        for name, text in (files or {}).items():
            with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
                handle.write(text)
        path = os.path.join(directory, "agents.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        return server.load_registry(path)

    def assert_invalid(self, config, fragment, files=None):
        with self.assertRaises(ValueError) as caught:
            self.load(config, files)
        self.assertIn(fragment, str(caught.exception))

    def test_a_registry_without_roles_still_loads(self):
        self.assertIsNone(self.load({"agents": [fake()]})["fake"]["_role"])

    def test_every_role_problem_is_a_registry_error_that_names_the_role(self):
        def one(**spec):
            return registry([fake(role="reviewer")], {"reviewer": spec})

        cases = [
            (registry([fake(role="auditor")]), "no role auditor", None),
            (one(description="d", prompt="p", promptFile="r.md"), "exactly one of prompt", None),
            (one(description="d"), "exactly one of prompt", None),
            (one(description="d", promptFile="gone.md"), "cannot read promptFile", None),
            (one(description="d", promptFile="empty.md"), "instructions are empty", {"empty.md": "\n\n"}),
            (one(description="d", prompt="   "), "instructions are empty", None),
            (one(prompt="p"), "description must be a non-empty string", None),
            (one(description="d", prompt="p", systemPrompt="x"), "unknown field systemPrompt", None),
            (one(description="d", prompt="p", delivery="inline"), "delivery must be prepend", None),
            (one(description="d", prompt="p", delivery="adapter"),
             'asks for "delivery": "adapter"', None),
            (one(description="d", prompt="x" * (server.MAX_ROLE_PROMPT + 1)),
             "AGENT_RELAY_MAX_ROLE_PROMPT_BYTES", None),
        ]
        for config, fragment, files in cases:
            with self.subTest(fragment=fragment):
                self.assert_invalid(config, fragment, files)

    def test_a_queued_task_whose_instructions_grew_fails_at_dispatch_instead_of_running(self):
        """The registry may be reloaded while a task waits, so dispatch re-checks the budget."""
        agent = self.load(registry([fake(role="reviewer")]))["fake"]
        task = {"id": "t", "agentId": "fake", "input": "a short input"}
        self.assertIsNone(server._dispatch_plan(agent, task)[2])
        grown = {**agent, "_role": {**agent["_role"],
                                   "instructions": "z" * (server.MAX_COMMAND_INPUT + 1)}}
        self.assertEqual(server._dispatch_plan(grown, task)[2][0], "role_input_too_large")


class ExampleRegistryTests(unittest.TestCase):
    def test_the_example_registry_loads_and_shows_a_role_worker(self):
        agents = server.load_registry(EXAMPLE_REGISTRY)
        roles = {agent["role"] for agent in agents.values() if agent.get("role")}
        self.assertTrue(roles, "config/agents.example.json should show at least one role worker")
        for agent in agents.values():
            if agent.get("role"):
                self.assertTrue(agent["_role"]["instructions"].strip())
