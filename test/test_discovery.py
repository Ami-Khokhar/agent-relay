"""Discovery tests: where the token lives, and which model each registered agent runs."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_helpers import PYTHON, Relay

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import server


class TokenDiscoveryTests(unittest.TestCase):
    def test_the_unauthorized_reply_names_the_default_token_file(self):
        home = tempfile.mkdtemp(prefix="a2a-auth-home-")
        self.addCleanup(shutil.rmtree, home, True)
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print(1)"]},
                      env={"AGENT_RELAY_TOKEN": "", "HOME": home, "USERPROFILE": home})
        try:
            status, payload = relay.request("GET", "/v1/agents", token=None)
            self.assertEqual(status, 401)
            self.assertEqual(payload["error"], "unauthorized")
            self.assertIn("~/.config/agent-relay/token", payload["message"])
            self.assertIn(os.path.join(home, ".config", "agent-relay", "token"),
                          payload["message"])
        finally:
            relay.close()

    def test_the_unauthorized_reply_names_a_configured_token_file(self):
        directory = tempfile.mkdtemp(prefix="a2a-auth-file-")
        self.addCleanup(shutil.rmtree, directory, True)
        token_file = os.path.join(directory, "token")
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print(1)"]},
                      env={"AGENT_RELAY_TOKEN": "", "AGENT_RELAY_TOKEN_FILE": token_file})
        try:
            status, payload = relay.request("GET", "/v1/agents", token=None)
            self.assertEqual(status, 401)
            self.assertEqual(payload["error"], "unauthorized")
            self.assertIn(token_file, payload["message"])
            # Even a configured file points at where the token would otherwise live.
            self.assertIn("~/.config/agent-relay/token", payload["message"])
        finally:
            relay.close()


class AgentMetadataTests(unittest.TestCase):
    def test_the_listing_returns_model_and_tags_only_when_declared(self):
        relay = Relay([
            {"id": "glm-factory", "name": "OpenCode (GLM 5.3 Flash), sandboxed factory worker",
             "command": PYTHON, "args": ["-c", "print(1)"],
             "model": "glm-5.3-flash", "tags": ["reviewer", "factory"]},
            {"id": "pi-glm", "command": PYTHON, "args": ["-c", "print(1)"]},
        ])
        try:
            status, listing = relay.request("GET", "/v1/agents")
            self.assertEqual(status, 200)
            entries = {agent["id"]: agent for agent in listing["agents"]}
            self.assertEqual(entries["glm-factory"]["model"], "glm-5.3-flash")
            self.assertEqual(entries["glm-factory"]["tags"], ["reviewer", "factory"])
            self.assertNotIn("model", entries["pi-glm"])
            self.assertNotIn("tags", entries["pi-glm"])
        finally:
            relay.close()

    def test_a_tag_filter_lists_only_the_agents_carrying_the_tag(self):
        relay = Relay([
            {"id": "glm-factory", "command": PYTHON, "args": ["-c", "print(1)"],
             "tags": ["reviewer"]},
            {"id": "pi-mimo", "command": PYTHON, "args": ["-c", "print(1)"],
             "tags": ["reviewer", "factory"]},
            {"id": "plain", "command": PYTHON, "args": ["-c", "print(1)"]},
        ])
        try:
            status, listing = relay.request("GET", "/v1/agents?tag=reviewer")
            self.assertEqual(status, 200)
            self.assertEqual({agent["id"] for agent in listing["agents"]},
                             {"glm-factory", "pi-mimo"})

            # Every named tag must be carried, so an any-match filter would list both agents.
            status, listing = relay.request("GET", "/v1/agents?tag=reviewer&tag=factory")
            self.assertEqual(status, 200)
            self.assertEqual([agent["id"] for agent in listing["agents"]], ["pi-mimo"])

            status, listing = relay.request("GET", "/v1/agents?tag=nobody")
            self.assertEqual(status, 200)
            self.assertEqual(listing["agents"], [])

            # A blank tag is a tag no agent carries, not an omitted filter.
            status, listing = relay.request("GET", "/v1/agents?tag=")
            self.assertEqual(status, 200)
            self.assertEqual(listing["agents"], [])

            _, all_agents = relay.request("GET", "/v1/agents")
            self.assertEqual({agent["id"] for agent in all_agents["agents"]},
                             {"glm-factory", "pi-mimo", "plain"})
        finally:
            relay.close()

    def test_the_listed_metadata_is_the_trimmed_value_that_was_validated(self):
        relay = Relay([
            {"id": "glm-factory", "command": PYTHON, "args": ["-c", "print(1)"],
             "model": " glm-5.3-flash ", "tags": [" reviewer ", "factory"]},
        ])
        try:
            status, listing = relay.request("GET", "/v1/agents")
            self.assertEqual(status, 200)
            entry = listing["agents"][0]
            self.assertEqual(entry["model"], "glm-5.3-flash")
            self.assertEqual(entry["tags"], ["reviewer", "factory"])
            # The trimmed value is also the one the filter matches.
            status, listing = relay.request("GET", "/v1/agents?tag=reviewer")
            self.assertEqual(status, 200)
            self.assertEqual([agent["id"] for agent in listing["agents"]], ["glm-factory"])
        finally:
            relay.close()

    def test_a_registry_that_mistypes_model_or_tags_is_refused_at_load(self):
        directory = tempfile.mkdtemp(prefix="a2a-meta-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "agents.json")
        for field, value in (("model", ""), ("model", "   "), ("model", 7), ("tags", "reviewer"),
                             ("tags", [""]), ("tags", [" "]), ("tags", [1])):
            with self.subTest(field=field, value=value):
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump({"agents": [{"id": "x", "command": PYTHON, "args": [],
                                           field: value}]}, handle)
                with self.assertRaises(ValueError) as raised:
                    server.load_registry(path)
                self.assertIn(field, str(raised.exception))

    def test_a_whitespace_only_value_is_refused_as_blank_not_empty(self):
        directory = tempfile.mkdtemp(prefix="a2a-blank-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "agents.json")
        for field, value, message in (
                ("model", "   ", "model must be a non-blank string"),
                ("tags", [" ", "\t"], "tags must be an array of non-blank strings")):
            with self.subTest(field=field):
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump({"agents": [{"id": "x", "command": PYTHON, "args": [],
                                           field: value}]}, handle)
                with self.assertRaises(ValueError) as raised:
                    server.load_registry(path)
                self.assertIn(message, str(raised.exception))
                self.assertNotIn("non-empty", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
