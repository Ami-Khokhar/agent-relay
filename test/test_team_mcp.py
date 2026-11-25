"""MCP team tools remain a thin, scoped client for /v1/teams."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import mcp_server
from relay_helpers import Relay


class TeamMcpTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.url = "http://127.0.0.1:43124"
        self.handle = mcp_server.create_http_handler(self.url, timeout=2)
        outer = self

        class Response:
            status = 200

            def __init__(self, value):
                self.data = json.dumps(value).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return self.data

        class Opener:
            def open(self, request, timeout):
                body = json.loads(request.data) if request.data else None
                path = request.full_url[len(outer.url):]
                outer.calls.append((request.get_method(), path, request.get_header("Authorization"), body))
                if path.startswith("/v1/teams/team-1/inbox?"):
                    value = {"events": []}
                elif path == "/v1/teams" and request.get_method() == "POST":
                    value = {"team": {"id": "team-1", "objective": body["objective"]}}
                elif path == "/v1/teams":
                    value = {"teams": [{"id": "team-1"}]}
                else:
                    value = {"ok": True, "team": {"id": "team-1"}}
                return Response(value)

        self.opener_patch = patch("mcp_server.urlrequest.build_opener", return_value=Opener())
        self.opener_patch.start()

    def tearDown(self):
        self.opener_patch.stop()

    def call(self, name, arguments):
        return self.handle({"method": "tools/call", "params": {"name": name, "arguments": arguments}})

    def test_team_tools_proxy_run_operations_with_scoped_credential(self):
        env = {"AGENT_RELAY_TEAM_TOKEN": "participant-secret", "AGENT_RELAY_TOKEN": "owner-secret"}
        with patch.dict(os.environ, env):
            created = self.call("create_team", {
                "objective": "Improve the product",
                "participants": [{"id": "worker", "agentId": "fake", "role": "worker"}],
            })
            self.assertEqual(created["structuredContent"]["team"]["id"], "team-1")
            self.call("assign_team_work", {"teamId": "team-1", "participantId": "worker",
                                            "assignment": "Implement the API"})
            self.call("post_team_message", {"teamId": "team-1", "recipientId": "lead",
                                             "body": "I found a concern", "kind": "question"})

        self.assertEqual([item[2] for item in self.calls], [
            "Bearer participant-secret", "Bearer participant-secret", "Bearer participant-secret",
        ])
        self.assertEqual(self.calls[1][3], {"participantId": "worker", "assignment": "Implement the API"})
        self.assertEqual(self.calls[2][3], {"recipientId": "lead", "body": "I found a concern", "kind": "question"})

    def test_team_credential_never_authenticates_legacy_routes_and_no_mint_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = os.path.join(directory, "token")
            with open(token_file, "w", encoding="utf-8") as handle:
                handle.write("owner-from-token-file")
            with patch.dict(os.environ, {"AGENT_RELAY_TEAM_TOKEN": "participant-secret",
                                         "AGENT_RELAY_TOKEN_FILE": token_file,
                                         "AGENT_RELAY_TOKEN": ""}):
                agents = self.handle({"method": "tools/call", "params": {"name": "list_agents", "arguments": {}}})
        # The fake returns a team-shaped object, but the key invariant here is the credential
        # attached to the legacy path.
        self.assertFalse(agents.get("isError", False))
        self.assertEqual(self.calls[0][1], "/v1/agents")
        self.assertIsNone(self.calls[0][2])
        tools = {tool["name"] for tool in self.handle({"method": "tools/list"})["tools"]}
        self.assertNotIn("create_team_credential", tools)
        self.assertNotIn("mint_team_token", tools)

    def test_real_team_http_roundtrip_and_worker_token_scope(self):
        # urllib.request is shared with the relay helper; the fake opener used by
        # the other tests would intercept Relay._wait_ready() too.
        self.opener_patch.stop()
        state_dir = tempfile.TemporaryDirectory(prefix="team-mcp-state-")
        self.addCleanup(state_dir.cleanup)
        try:
            relay = Relay({"id": "fake", "command": sys.executable,
                           "args": ["-c", "print('unused')"]},
                          env={"AGENT_RELAY_STATE_DIR": state_dir.name})
        except PermissionError as error:
            state_dir.cleanup()
            self.skipTest(f"sandbox does not permit local HTTP sockets: {error}")
        self.addCleanup(relay.close)
        handle = mcp_server.create_http_handler(relay.base, timeout=3)
        with patch.dict(os.environ, {"AGENT_RELAY_TOKEN": "relay-test-token"}):
            created = handle({"method": "tools/call", "params": {"name": "create_team", "arguments": {
                "objective": "Run a durable discussion",
                "participants": [{"id": "worker-one", "agentId": "fake", "role": "implementer"}],
            }}})
            self.assertIn("team", created.get("structuredContent", {}), created)
            team = created["structuredContent"]["team"]
            team_id = team["id"]
            assigned = handle({"method": "tools/call", "params": {"name": "assign_team_work", "arguments": {
                "teamId": team_id, "participantId": "worker-one", "assignment": "Investigate the API",
            }}})
            self.assertTrue(assigned["structuredContent"]["ok"])
            assignment_id = assigned["structuredContent"]["team"]["assignments"][0]["id"]
            approved = handle({"method": "tools/call", "params": {"name": "set_team_phase", "arguments": {
                "teamId": team_id, "phase": "specification", "spec": "Document the API", "approved": True,
            }}})
            self.assertTrue(approved["structuredContent"]["ok"])
            self.assertFalse(handle({"method": "tools/call", "params": {"name": "set_team_summary", "arguments": {
                "teamId": team_id, "summary": "Discovery is complete",
            }}}).get("isError", False))
            self.assertFalse(handle({"method": "tools/call", "params": {"name": "ask_team_user_question", "arguments": {
                "teamId": team_id, "question": "Which target should we support?",
            }}}).get("isError", False))
            self.assertFalse(handle({"method": "tools/call", "params": {"name": "add_team_artifact", "arguments": {
                "teamId": team_id, "kind": "spec", "title": "API draft", "uri": "docs/api.md",
            }}}).get("isError", False))
            question_result = handle({"method": "tools/call", "params": {"name": "get_team_questions", "arguments": {"teamId": team_id}}})
            question_id = question_result["structuredContent"]["questions"][0]["id"]
            answered = handle({"method": "tools/call", "params": {"name": "answer_team_user_question", "arguments": {
                "teamId": team_id, "questionId": question_id, "answer": "Target A",
            }}})
            self.assertTrue(answered["structuredContent"]["ok"])

        status, credential = relay.request("POST", f"/v1/teams/{team_id}/credentials",
                                           {"participantId": "worker-one"})
        self.assertEqual(status, 201)
        with patch.dict(os.environ, {"AGENT_RELAY_TEAM_TOKEN": credential["token"],
                                     "AGENT_RELAY_TOKEN": "relay-test-token"}):
            worker_inbox = handle({"method": "tools/call", "params": {"name": "wait_team_messages", "arguments": {
                "teamId": team_id, "maxWaitMs": 100,
            }}})
            assignment_event = next(event for event in worker_inbox["structuredContent"]["events"] if event["kind"] == "assignment")
            worker_ack = handle({"method": "tools/call", "params": {"name": "ack_team_delivery", "arguments": {
                "teamId": team_id, "eventId": assignment_event["id"],
            }}})
            self.assertTrue(worker_ack["structuredContent"]["acknowledged"])
            posted = handle({"method": "tools/call", "params": {"name": "post_team_message", "arguments": {
                "teamId": team_id, "recipientId": "lead", "body": "I found evidence",
                "assignmentId": assignment_id, "kind": "question",
            }}})
            self.assertTrue(posted["structuredContent"]["ok"])
            legacy = handle({"method": "tools/call", "params": {"name": "list_agents", "arguments": {}}})
            self.assertTrue(legacy["isError"])
            read = handle({"method": "tools/call", "params": {"name": "get_team", "arguments": {"teamId": team_id}}})
        saved = read["structuredContent"]["team"]
        self.assertEqual(saved["phase"], "specification")
        self.assertTrue(saved["approved"])
        self.assertEqual(next(event["body"] for event in saved["events"] if event["kind"] == "question"),
                         "I found evidence")
        self.assertEqual(saved["assignments"][0]["text"], "Investigate the API")
        self.assertEqual(saved["summary"]["text"], "Discovery is complete")
        self.assertEqual(saved["artifacts"][0]["title"], "API draft")
        self.assertFalse(saved["questions"])
        with patch.dict(os.environ, {"AGENT_RELAY_TOKEN": "relay-test-token"}):
            owner_inbox = handle({"method": "tools/call", "params": {"name": "wait_team_messages", "arguments": {
                "teamId": team_id, "maxWaitMs": 100,
            }}})
            question_message = next(event for event in owner_inbox["structuredContent"]["events"] if event["body"] == "I found evidence")
            lead_ack = handle({"method": "tools/call", "params": {"name": "ack_team_delivery", "arguments": {
                "teamId": team_id, "eventId": question_message["id"],
            }}})
        self.assertTrue(lead_ack["structuredContent"]["acknowledged"])

    def test_owner_can_list_runs_with_owner_credential(self):
        with patch.dict(os.environ, {"AGENT_RELAY_TOKEN": "owner-secret"}):
            result = self.call("list_teams", {})
        self.assertEqual(result["structuredContent"]["teams"][0]["id"], "team-1")
        self.assertEqual(self.calls[0][1], "/v1/teams")
        self.assertEqual(self.calls[0][2], "Bearer owner-secret")

    def test_runtime_tools_map_to_team_dispatch_delivery_and_artifact_routes(self):
        with patch.dict(os.environ, {"AGENT_RELAY_TOKEN": "owner-secret"}):
            operations = [
                ("dispatch_team_assignment", {"teamId": "team-1", "assignmentId": "a1", "requestId": "r1"}),
                ("get_team_inbox", {"teamId": "team-1", "unreadOnly": True}),
                ("wait_team_messages", {"teamId": "team-1"}),
                ("ack_team_delivery", {"teamId": "team-1", "eventId": "e1"}),
                ("get_team_attempts", {"teamId": "team-1"}),
                ("set_team_summary", {"teamId": "team-1", "summary": "On track"}),
                ("get_team_summary", {"teamId": "team-1"}),
                ("get_team_questions", {"teamId": "team-1"}),
                ("ask_team_user_question", {"teamId": "team-1", "question": "Which target?"}),
                ("answer_team_user_question", {"teamId": "team-1", "questionId": "q/1", "answer": "Target A"}),
                ("get_team_artifacts", {"teamId": "team-1"}),
                ("add_team_artifact", {"teamId": "team-1", "kind": "spec", "title": "API spec", "uri": "docs/spec.md"}),
                ("export_team_history", {"teamId": "team-1"}),
            ]
            for name, args in operations:
                result = self.call(name, args)
                self.assertFalse(result.get("isError", False), result)
        self.assertEqual([(method, path) for method, path, _token, _body in self.calls], [
            ("POST", "/v1/teams/team-1/dispatch"),
            ("GET", "/v1/teams/team-1/inbox?unreadOnly=true"),
            ("GET", "/v1/teams/team-1/inbox?unreadOnly=true"),
            ("POST", "/v1/teams/team-1/ack"),
            ("GET", "/v1/teams/team-1/attempts"),
            ("POST", "/v1/teams/team-1/summary"),
            ("GET", "/v1/teams/team-1/summary"),
            ("GET", "/v1/teams/team-1/questions"),
            ("POST", "/v1/teams/team-1/questions"),
            ("POST", "/v1/teams/team-1/questions/q%2F1"),
            ("GET", "/v1/teams/team-1/artifacts"),
            ("POST", "/v1/teams/team-1/artifacts"),
            ("POST", "/v1/teams/team-1/history"),
        ])
        self.assertEqual(self.calls[0][3], {"assignmentId": "a1", "requestId": "r1"})
        self.assertEqual(self.calls[9][3], {"answer": "Target A"})

    def test_wait_team_messages_bounds_empty_inbox_polling(self):
        result = self.call("wait_team_messages", {"teamId": "team-1", "maxWaitMs": 20})
        self.assertTrue(result["structuredContent"]["waitExpired"])
        self.assertGreaterEqual(result["structuredContent"]["waitedMs"], 10)
        self.assertLessEqual(result["structuredContent"]["waitedMs"], 1000)
        self.assertGreaterEqual(len(self.calls), 1)
        with self.assertRaises(mcp_server.RpcError):
            self.call("wait_team_messages", {"teamId": "team-1", "maxWaitMs": 30001})

    def test_unknown_team_arguments_are_rejected_before_request(self):
        with self.assertRaises(mcp_server.RpcError):
            self.call("create_team", {"objective": "x", "participants": [{"id": "lead"}], "token": "leak"})
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
