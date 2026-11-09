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
from relay_helpers import PRINT_ARGV, PYTHON, Relay, send_json, start_http_server, stop_http_server
import server

REVIEWER = "You review the change against the intent that was stated.\nReport defects only."
IMPLEMENTER = "You implement the change and prove it."
ROLES = {
    "reviewer": {"description": "Reviews a diff; never edits.",
                 "prompt": REVIEWER, "inputHeader": "Review this work:"},
    "implementer": {"description": "Implements the change.", "prompt": IMPLEMENTER},
}
# A stdio adapter that answers with the relay.adapter/v1 document it received.
STDIO_ECHO = ("import json, sys\n"
              "doc = json.load(sys.stdin)\n"
              "print(json.dumps({'protocolVersion': 'relay.adapter/v1', 'status': 'completed',\n"
              "                  'output': json.dumps(doc)}))\n")
EXAMPLE_REGISTRY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "agents.example.json")


def fake(**extra):
    """A command agent that prints the arguments it received."""
    return {"id": "fake", "command": PYTHON, "args": [*PRINT_ARGV], **extra}


def slow():
    """A command agent that holds its slot long enough for a second task to stay queued."""
    return {"id": "slow", "command": PYTHON, "args": ["-c", "import time; time.sleep(3)"]}


def registry(agents, roles=ROLES, **top):
    return {"agents": agents if isinstance(agents, list) else [agents], "roles": roles, **top}


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


class RolesTests(unittest.TestCase):
    def start(self, agents, roles=ROLES, env=None):
        full = isinstance(agents, dict) and "agents" in agents
        relay = Relay(agents if full else registry(agents, roles), env=env)
        self.addCleanup(relay.close)
        return relay

    def argv_of(self, relay, **body):
        task = relay.run_task(agentId="fake", **body)
        self.assertEqual(task["status"], "completed", task)
        return json.loads(task["output"])

    def test_a_command_worker_receives_the_instructions_and_the_record_names_them(self):
        relay = self.start(fake(role="reviewer"))
        task = relay.run_task(agentId="fake", input="check the diff")
        self.assertEqual(json.loads(task["output"]),
                         [f"{REVIEWER}\n\nReview this work:\n\ncheck the diff"])
        self.assertEqual((task["role"], task["rolePromptSha256"]), ("reviewer", sha(REVIEWER)))
        self.assertEqual(task["inputChars"], len("check the diff"))
        self.assertNotIn(REVIEWER, json.dumps(task))

    def test_a_worker_without_a_role_runs_exactly_as_before(self):
        relay = self.start(fake())
        task = relay.run_task(agentId="fake", input="plain")
        self.assertEqual(json.loads(task["output"]), ["plain"])
        self.assertNotIn("role", task)
        self.assertNotIn("rolePromptSha256", task)

    def test_the_listing_shows_the_role_so_a_caller_can_choose_by_role(self):
        relay = self.start([fake(role="reviewer"),
                            {"id": "other", "command": PYTHON, "args": PRINT_ARGV}])
        _, payload = relay.request("GET", "/v1/agents")
        by_id = {entry["id"]: entry for entry in payload["agents"]}
        self.assertEqual(by_id["fake"]["role"], "reviewer")
        self.assertEqual(by_id["fake"]["roleDescription"], "Reviews a diff; never edits.")
        self.assertNotIn("role", by_id["other"])
        self.assertNotIn(REVIEWER, json.dumps(payload))
        self.assertEqual(relay.request("GET", "/healthz")[1]["limits"]["maxRolePromptBytes"],
                         server.MAX_ROLE_PROMPT)

    def test_the_command_line_budget_counts_the_instructions(self):
        relay = self.start([fake(role="reviewer")],
                           {"reviewer": {"description": "d", "prompt": "r" * 200}},
                           env={"AGENT_RELAY_MAX_COMMAND_INPUT_BYTES": "400"})
        status, payload = relay.submit(agentId="fake", input="x" * 199)
        self.assertEqual((status, payload["error"]), (413, "role_input_too_large"))
        self.assertIn("AGENT_RELAY_MAX_COMMAND_INPUT_BYTES", payload["message"])
        self.assertEqual(relay.request("GET", "/v1/tasks")[1]["tasks"], [])
        status, submitted = relay.submit(agentId="fake", input="y" * 198)
        self.assertEqual(status, 202, submitted)
        self.assertEqual(relay.wait_task(submitted["id"])["status"], "completed")

    def test_prompt_file_is_read_from_the_registry_directory(self):
        relay = Relay(registry([fake(role="reviewer")],
                               {"reviewer": {"description": "d", "promptFile": "r.md"}}),
                      files={"r.md": "from the file\n"})
        self.addCleanup(relay.close)
        self.assertEqual(self.argv_of(relay, input="x"), ["from the file\n\nx"])

    def test_reload_replaces_the_instructions_and_the_digest(self):
        relay = self.start(fake(role="reviewer"))
        self.assertEqual(self.argv_of(relay, input="x")[0], f"{REVIEWER}\n\nReview this work:\n\nx")
        relay.write_registry(registry([fake(role="reviewer")],
                                      {"reviewer": {"description": "d", "prompt": "new order"}}))
        relay.reload()
        task = relay.run_task(agentId="fake", input="x")
        self.assertEqual(json.loads(task["output"]), ["new order\n\nx"])
        self.assertEqual(task["rolePromptSha256"], sha("new order"))

    def test_an_adapter_delivery_role_travels_as_its_own_field(self):
        captured = {}

        def handler(request):
            captured.update(json.loads(request.rfile.read(int(request.headers["content-length"]))))
            send_json(request, 200, {"protocolVersion": "relay.adapter/v1", "status": "completed"})

        http_server, port = start_http_server(handler)
        self.addCleanup(stop_http_server, http_server)
        relay = self.start([{"id": "fake", "type": "http", "url": f"http://127.0.0.1:{port}/run",
                             "role": "reviewer"}],
                           {"reviewer": {"description": "d", "prompt": REVIEWER,
                                         "delivery": "adapter"}})
        self.assertEqual(relay.run_task(agentId="fake", input="the input")["status"], "completed")
        self.assertEqual(captured["role"], {"id": "reviewer", "instructions": REVIEWER})
        self.assertEqual(captured["task"]["input"], "the input")

    def test_a_stdio_worker_with_prepend_delivery_gets_the_instructions_in_its_input(self):
        relay = self.start({"id": "fake", "type": "stdio", "command": PYTHON,
                            "args": ["-c", STDIO_ECHO], "role": "implementer"})
        document = json.loads(relay.run_task(agentId="fake", input="build it")["output"])
        self.assertEqual(document["task"]["input"], f"{IMPLEMENTER}\n\nbuild it")
        self.assertNotIn("role", document)

    def test_a_read_only_role_worker_accepts_an_input_that_starts_with_a_dash(self):
        """The instructions precede the input on the command line, so it cannot be read as an option."""
        relay = self.start(fake(role="reviewer", readOnly={"args": ["--access=read"]}))
        status, submitted = relay.submit(agentId="fake", input="--access=write", readOnly=True)
        self.assertEqual(status, 202, submitted)
        self.assertEqual(json.loads(relay.wait_task(submitted["id"])["output"]),
                         ["--access=read", f"{REVIEWER}\n\nReview this work:\n\n--access=write"])

    def test_a_queued_task_follows_the_entry_that_dispatches_it(self):
        """A reload can replace, lengthen or drop the role of a task that is still waiting."""
        review = {"reviewer": {"description": "d", "prompt": "r" * 200}}
        cases = [
            ("longer instructions",
             registry([fake(role="reviewer")],
                      {"reviewer": {"description": "d", "prompt": "new order"}}), {}, None),
            ("instructions that no longer fit",
             registry([fake(role="reviewer")],
                      {"reviewer": {"description": "d", "prompt": "r" * 380}}),
             {"AGENT_RELAY_MAX_COMMAND_INPUT_BYTES": "400"}, "role_input_too_large"),
            ("a role that is gone", registry([fake()], {}),
             {"AGENT_RELAY_MAX_COMMAND_INPUT_BYTES": "400"}, "role_removed"),
        ]
        for label, reloaded, extra, expected_code in cases:
            with self.subTest(label=label):
                relay = self.start([fake(role="reviewer"), slow()], review,
                                   env={"AGENT_RELAY_MAX_ACTIVE": "1", **extra})
                relay.submit(agentId="slow", input="hold the slot")
                status, queued = relay.submit(agentId="fake", input="x" * 100)
                self.assertEqual(status, 202, queued)
                relay.write_registry(reloaded)
                relay.reload()
                task = relay.wait_task(queued["id"], timeout=20)
                if expected_code is None:
                    self.assertEqual(json.loads(task["output"]), ["new order\n\n" + "x" * 100])
                    self.assertEqual(task["rolePromptSha256"], sha("new order"))
                else:
                    self.assertEqual(task["status"], "failed", task)
                    self.assertTrue(task["error"].startswith(expected_code + ":"), task["error"])


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
            (one(description="d", promptFile="empty.md"), "instructions are empty",
             {"empty.md": "\n\n"}),
            (one(description="d", prompt="   "), "instructions are empty", None),
            (one(prompt="p"), "description must be a non-empty string", None),
            (one(description="d", prompt="p", systemPrompt="x"), "unknown field systemPrompt", None),
            (one(description="d", prompt="p", delivery="inline"), "delivery must be prepend", None),
            (one(description="d", prompt="p", delivery="adapter", inputHeader="Review:"),
             "inputHeader needs", None),
            (one(description="d", prompt="p", delivery="adapter"),
             'asks for "delivery": "adapter"', None),
            (one(description="d", prompt="x" * (server.MAX_ROLE_PROMPT + 1)),
             "AGENT_RELAY_MAX_ROLE_PROMPT_BYTES", None),
        ]
        for config, fragment, files in cases:
            with self.subTest(fragment=fragment):
                with self.assertRaises(ValueError) as caught:
                    self.load(config, files)
                self.assertIn(fragment, str(caught.exception))

    def test_readme_worker_roles_load_with_distinct_roles_and_model_arguments(self):
        with open(os.path.join(os.path.dirname(EXAMPLE_REGISTRY), "..", "README.md"),
                  encoding="utf-8") as handle:
            section = handle.read().split("### Worker roles\n", 1)[1]
        example = json.loads(section.split("```json\n", 1)[1].split("\n```", 1)[0])
        agents = self.load(example)
        by_role = {agent["role"]: agent for agent in agents.values()}
        self.assertEqual(set(by_role), {"reviewer", "implementer"})
        args = by_role["implementer"]["args"]
        self.assertNotEqual(args, by_role["reviewer"]["args"])
        self.assertIn("--model", args)
        model_index = args.index("--model") + 1
        self.assertLess(model_index, len(args))
        self.assertTrue(args[model_index].strip())

    def test_the_shipped_example_loads_where_it_is_and_after_a_copy(self):
        """A user or setup.sh copies the example to the per-user config dir, so the copy must load."""
        expected = {a["role"] for a in server.load_registry(EXAMPLE_REGISTRY).values() if a.get("role")}
        self.assertTrue(expected, "config/agents.example.json should show role workers")
        directory = tempfile.mkdtemp(prefix="agent-relay-example-")
        self.addCleanup(shutil.rmtree, directory, True)
        copied = os.path.join(directory, "agents.json")
        shutil.copy(EXAMPLE_REGISTRY, copied)
        agents = server.load_registry(copied)
        self.assertEqual({a["role"] for a in agents.values() if a.get("role")}, expected)
        for agent in agents.values():
            self.assertTrue(not agent.get("role") or agent["_role"]["instructions"].strip())
