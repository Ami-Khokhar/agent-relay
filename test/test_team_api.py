"""HTTP boundaries for durable team coordination."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import server
from teams import TeamStore


class TeamAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {name: getattr(server, name) for name in
                    ("TOKEN", "STATE_DIR", "TEAM_STORE", "TEAM_EXECUTOR", "TEAM_WAKER",
                     "TEAM_CREDENTIALS", "TEAM_RECONCILER_STARTED", "TEAM_RECOVERY_DONE",
                     "AGENTS", "ALLOWED_ORIGINS")}
        server.TOKEN = "owner-secret"
        server.STATE_DIR = self.tmp.name
        server.TEAM_STORE = TeamStore(os.path.join(self.tmp.name, "teams.sqlite3"))
        server.TEAM_EXECUTOR = None
        server.TEAM_WAKER = None
        server.TEAM_RECONCILER_STARTED = True
        server.TEAM_RECOVERY_DONE = True
        server.TEAM_CREDENTIALS = {}
        server.AGENTS = {"worker-config": {"id": "worker-config", "type": "codex", "model": "m"}}
        server.ALLOWED_ORIGINS = set()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%s" % self.httpd.server_port

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        for name, value in self.old.items():
            setattr(server, name, value)
        self.tmp.cleanup()

    def call(self, method, path, token="owner-secret", body=None, origin=None):
        headers = {}
        if token is not None:
            headers["authorization"] = "Bearer " + token
        if body is not None:
            headers["content-type"] = "application/json"
        if origin is not None:
            headers["origin"] = origin
        data = json.dumps(body).encode() if body is not None else None
        req = Request(self.base + path, data=data, method=method, headers=headers)
        try:
            response = urlopen(req, timeout=3)
        except HTTPError as error:
            self.last_headers = error.headers
            return error.code, json.loads(error.read()) if "json" in error.headers.get("content-type", "") else {}
        self.last_headers = response.headers
        raw = response.read()
        return response.status, json.loads(raw) if raw and "json" in response.headers.get("content-type", "") else raw

    def create_team(self):
        status, result = self.call("POST", "/v1/teams", body={
            "objective": "Review this change",
            "participants": [{"id": "reviewer-a", "agentId": "worker-config", "role": "reviewer"}],
        })
        self.assertEqual(status, 201, result)
        return result["team"]["id"]

    def credential(self, run_id, participant_id="reviewer-a"):
        status, result = self.call("POST", "/v1/teams/%s/credentials" % run_id,
                                   body={"participantId": participant_id})
        self.assertEqual(status, 201, result)
        return result["token"]

    def test_worker_credential_is_scoped_and_actor_is_not_client_selected(self):
        run_id = self.create_team()
        token = self.credential(run_id)
        status, result = self.call("POST", "/v1/teams/%s/assignments" % run_id,
                                   token=token, body={"participantId": "reviewer-a", "assignment": "review"})
        self.assertEqual(status, 403)
        self.assertEqual(result["error"], "forbidden")

        status, assigned = self.call("POST", "/v1/teams/%s/assignments" % run_id,
                                     body={"participantId": "reviewer-a", "assignment": "review"})
        self.assertEqual(status, 200, assigned)
        assignment_id = assigned["team"]["assignments"][0]["id"]
        status, posted = self.call("POST", "/v1/teams/%s/messages" % run_id, token=token,
                                   body={"recipientId": "lead", "body": "Evidence attached",
                                         "assignmentId": assignment_id, "actor": "lead"})
        self.assertEqual(status, 200, posted)
        self.assertEqual(posted["team"]["events"][-1]["actor"], "reviewer-a")
        self.assertEqual(self.call("GET", "/v1/agents", token=token)[0], 403)

    def test_creation_resolves_agent_and_rejects_unknown_agent_and_lead_credential(self):
        status, result = self.call("POST", "/v1/teams", body={
            "objective": "x", "participants": [{"id": "r", "agentId": "missing"}]})
        self.assertEqual(status, 400)
        self.assertEqual(result["error"], "unknown_agent")
        status, malformed = self.call("POST", "/v1/teams", body={
            "objective": "x", "participants": [{"id": "r", "agentId": []}]})
        self.assertEqual(status, 400)
        self.assertEqual(malformed["error"], "invalid_participant")
        run_id = self.create_team()
        self.assertEqual(self.call("POST", "/v1/teams/%s/credentials" % run_id,
                                   body={"participantId": "lead"})[0], 400)

    def test_missing_team_and_malformed_participant_ids_are_client_errors(self):
        self.assertEqual(self.call("GET", "/v1/teams/missing-run")[0], 404)
        run_id = self.create_team()
        status, result = self.call("POST", "/v1/teams/%s/credentials" % run_id,
                                   body={"participantId": []})
        self.assertEqual(status, 400)
        self.assertEqual(result["error"], "invalid_participant")

    def test_credentials_are_process_local_and_do_not_grant_other_run_access(self):
        first, second = self.create_team(), self.create_team()
        token = self.credential(first)
        self.assertEqual(self.call("GET", "/v1/teams/%s" % first, token=token)[0], 200)
        self.assertEqual(self.call("GET", "/v1/teams/%s" % second, token=token)[0], 403)
        # A fresh store instance sees the durable run, while a fresh process credential map
        # rejects the old bearer capability.
        server.TEAM_STORE = TeamStore(os.path.join(self.tmp.name, "teams.sqlite3"))
        server.TEAM_CREDENTIALS = {}
        self.assertEqual(self.call("GET", "/v1/teams/%s" % first, token=token)[0], 401)
        self.assertEqual(self.call("GET", "/v1/teams", token="owner-secret")[1]["teams"][0]["id"], first)

    def test_minting_replaces_the_participants_previous_credential(self):
        run_id = self.create_team()
        first = self.credential(run_id)
        second = self.credential(run_id)
        self.assertNotEqual(first, second)
        self.assertEqual(self.call("GET", "/v1/teams/%s" % run_id, token=first)[0], 401)
        self.assertEqual(self.call("GET", "/v1/teams/%s" % run_id, token=second)[0], 200)

    def test_reopen_is_owner_only_and_requires_a_working_phase(self):
        run_id = self.create_team()
        token = self.credential(run_id)
        self.assertEqual(self.call("POST", "/v1/teams/%s/close" % run_id, body={})[0], 200)
        self.assertEqual(self.call("POST", "/v1/teams/%s/reopen" % run_id, token=token,
                                   body={"phase": "review"})[0], 403)
        self.assertEqual(self.call("POST", "/v1/teams/%s/reopen" % run_id, body={})[0], 400)
        self.assertEqual(self.call("POST", "/v1/teams/%s/reopen" % run_id,
                                   body={"phase": "complete"})[0], 400)
        status, reopened = self.call("POST", "/v1/teams/%s/reopen" % run_id, body={"phase": "review"})
        self.assertEqual(status, 200, reopened)
        self.assertFalse(reopened["team"]["closed"])

    def test_board_static_allowlist_is_same_origin_read_only_and_not_cached(self):
        status, page = self.call("GET", "/board/")
        self.assertEqual(status, 200)
        self.assertIn(b"kept in memory only", page)
        self.assertEqual(self.last_headers.get("cache-control"), "no-store")
        self.assertEqual(self.call("GET", "/board/", token=None,
                                   origin="https://other.example")[0], 403)
        self.assertEqual(self.call("GET", "/board/secret.json")[0], 404)
        self.assertEqual(self.call("POST", "/board/")[0], 405)


if __name__ == "__main__":
    unittest.main()
