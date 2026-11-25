"""Public HTTP integration for team task dispatch and participant capabilities."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import textwrap
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import server
from teams import TeamStore


class TeamExecutionAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {name: getattr(server, name) for name in
                    ("TOKEN", "STATE_DIR", "TEAM_STORE", "TEAM_EXECUTOR", "TEAM_CREDENTIALS",
                     "TEAM_WAKER",
                     "TEAM_RECONCILER_STARTED", "TEAM_RECOVERY_DONE", "AGENTS", "ALLOWED_ORIGINS",
                     "TASKS", "QUEUE", "REQUEST_IDS")}
        server.TOKEN = "owner-secret"
        server.STATE_DIR = __import__("pathlib").Path(self.tmp.name)
        server.TEAM_STORE = TeamStore(os.path.join(self.tmp.name, "teams.sqlite3"))
        server.TEAM_EXECUTOR = None
        server.TEAM_WAKER = None
        server.TEAM_CREDENTIALS = {}
        server.TEAM_RECONCILER_STARTED = True
        server.TEAM_RECOVERY_DONE = True
        server.TASKS, server.QUEUE, server.REQUEST_IDS = {}, [], {}
        server.AGENTS = {}
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

    def call(self, method, path, token="owner-secret", body=None):
        headers = {}
        if token is not None:
            headers["authorization"] = "Bearer " + token
        if body is not None:
            headers["content-type"] = "application/json"
        request = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                          method=method, headers=headers)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            return error.code, json.loads(error.read())
        raw = response.read()
        return response.status, json.loads(raw) if raw else {}

    def test_dispatch_runs_child_with_scoped_token_and_durable_result(self):
        script = textwrap.dedent("""\
            import json, os, urllib.request, urllib.error
            base = %r
            request = json.loads(input())
            run = os.environ['AGENT_RELAY_TEAM_ID']
            token = os.environ['AGENT_RELAY_TEAM_TOKEN']
            def call(method, path, body=None, bearer=token):
                headers = {'Authorization': 'Bearer ' + bearer}
                data = None
                if body is not None:
                    headers['Content-Type'] = 'application/json'
                    data = json.dumps(body).encode()
                return urllib.request.urlopen(urllib.request.Request(base + path, data=data,
                    method=method, headers=headers))
            team = json.load(call('GET', '/v1/teams/' + run))['team']
            assignment = next(a for a in team['assignments'] if a['participant_id'] != 'lead')
            call('POST', '/v1/teams/' + run + '/messages', {'recipientId': 'lead',
                'body': 'worker result', 'assignmentId': assignment['id']})
            try:
                call('POST', '/v1/teams/' + run + '/assignments',
                     {'participantId': 'worker', 'assignment': 'steal'})
                participant_authority = 200
            except urllib.error.HTTPError as error:
                participant_authority = error.code
            try:
                call('POST', '/v1/teams/' + run + '/assignments',
                     {'participantId': 'worker', 'assignment': 'steal'},
                     os.environ.get('AGENT_RELAY_TOKEN', 'missing-owner'))
                owner_authority = 200
            except urllib.error.HTTPError as error:
                owner_authority = error.code
            print(json.dumps({'protocolVersion': 'relay.adapter/v1', 'status': 'completed',
                              'output': str(participant_authority) + ':' + str(owner_authority)}))
        """ % self.base)
        server.AGENTS = {"worker": {"id": "worker", "type": "stdio", "command": sys.executable,
                                     "args": ["-c", script], "env": {"AGENT_RELAY_TOKEN": "owner-secret"},
                                     "inheritEnv": False, "timeoutMs": 10000,
                                     "readOnly": {"args": ["--read-only"]}}}
        code, result = self.call("POST", "/v1/teams", body={
            "objective": "integration", "participants": [{"id": "worker", "agentId": "worker"}]})
        self.assertEqual(code, 201, result)
        run_id = result["team"]["id"]
        _, assigned = self.call("POST", f"/v1/teams/{run_id}/assignments", body={
            "participantId": "worker", "assignment": "research", "purpose": "research"})
        assignment_id = assigned["team"]["assignments"][0]["id"]
        code, result = self.call("POST", f"/v1/teams/{run_id}/dispatch", body={
            "assignmentId": assignment_id, "requestId": "dispatch-once"})
        self.assertEqual(code, 202, result)
        task_id = result["attempt"]["task_id"]
        task = server._wait_for_terminal(task_id, 5000)
        self.assertEqual(task["status"], "completed", task)
        self.assertEqual(task["output"], "403:401")
        self.call("GET", f"/v1/teams/{run_id}")
        attempts = server.TEAM_STORE.list_attempts(run_id)
        self.assertEqual(attempts[0]["status"], "completed")
        credential_count = len(server.TEAM_CREDENTIALS)
        replay, _ = self.call("POST", f"/v1/teams/{run_id}/dispatch", body={
            "assignmentId": assignment_id, "requestId": "dispatch-once"})
        self.assertEqual(replay, 200)
        self.assertEqual(len(server.TEAM_CREDENTIALS), credential_count)
        inbox_code, inbox = self.call("GET", f"/v1/teams/{run_id}/inbox")
        self.assertEqual(inbox_code, 200, inbox)
        event = next(event for event in inbox["events"] if event["body"] == "worker result")
        ack_code, ack = self.call("POST", f"/v1/teams/{run_id}/ack", body={"eventId": event["id"]})
        self.assertEqual(ack_code, 200, ack)
        _, unread = self.call("GET", f"/v1/teams/{run_id}/inbox?unreadOnly=true")
        self.assertNotIn(event["id"], [item["id"] for item in unread["events"]])

    def test_owner_summary_question_artifact_and_history_routes(self):
        code, created = self.call("POST", "/v1/teams", body={"objective": "x", "participants": []})
        run_id = created["team"]["id"]
        self.assertEqual(self.call("POST", f"/v1/teams/{run_id}/summary", body={"summary": "Done"})[0], 200)
        _, question = self.call("POST", f"/v1/teams/{run_id}/questions", body={"question": "Which option?"})
        qid = question["team"]["questions"][0]["id"]
        self.assertEqual(self.call("POST", f"/v1/teams/{run_id}/questions/{qid}", body={"answer": "A"})[0], 200)
        self.assertEqual(self.call("POST", f"/v1/teams/{run_id}/artifacts", body={
            "kind": "report", "title": "Result", "uri": "runs/result.md"})[0], 200)
        _, view = self.call("GET", f"/v1/teams/{run_id}")
        self.assertEqual(view["team"]["summary"]["text"], "Done")
        self.assertEqual(view["team"]["questions"], [])
        self.assertEqual(len(view["team"]["artifacts"]), 1)
        self.assertEqual(self.call("POST", f"/v1/teams/{run_id}/history", body={})[0], 200)

    def test_addressed_tag_wakes_only_assigned_workers_after_prior_attempts_finish(self):
        script = textwrap.dedent("""\
            import json, os, sys, urllib.request
            base = %r
            request = json.loads(sys.stdin.readline())
            task = request['task']
            run = os.environ['AGENT_RELAY_TEAM_ID']
            participant = os.environ['AGENT_RELAY_TEAM_PARTICIPANT_ID']
            token = os.environ['AGENT_RELAY_TEAM_TOKEN']
            def post(path, body):
                data = json.dumps(body).encode()
                return urllib.request.urlopen(urllib.request.Request(base + path, data=data, method='POST',
                    headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}))
            team = json.load(urllib.request.urlopen(urllib.request.Request(base + '/v1/teams/' + run,
                headers={'Authorization': 'Bearer ' + token})))['team']
            assignment = next(a for a in team['assignments'] if a['participant_id'] == participant)
            if participant == 'b' and 'tag b after completion' in task['input']:
                post('/v1/teams/' + run + '/messages', {'recipientId': 'a', 'body': 'peer reply',
                    'assignmentId': assignment['id']})
            if participant == 'a' and 'peer reply' in task['input']:
                post('/v1/teams/' + run + '/messages', {'recipientId': 'lead', 'body': 'peer result integrated',
                    'assignmentId': assignment['id']})
                latest = json.load(urllib.request.urlopen(urllib.request.Request(base + '/v1/teams/' + run,
                    headers={'Authorization': 'Bearer ' + token})))['team']
                reply = next(e for e in latest['events'] if e['body'] == 'peer reply')
                post('/v1/teams/' + run + '/conclusions', {'threadId': reply['thread_id'],
                    'summary': 'Peer result verified', 'rationale': 'The reply addresses the assigned review.',
                    'action': 'Integrate the result', 'needsLead': True})
            print(json.dumps({'protocolVersion': 'relay.adapter/v1', 'status': 'completed',
                              'output': participant + ' completed'}))
        """ % self.base)
        server.AGENTS = {name: {"id": name, "type": "stdio", "command": sys.executable,
                                "args": ["-c", script], "inheritEnv": False,
                                "readOnly": {"args": ["--read-only"]}, "timeoutMs": 10000}
                         for name in ("agent-a", "agent-b")}
        _, created = self.call("POST", "/v1/teams", body={"objective": "peer flow", "participants": [
            {"id": "a", "agentId": "agent-a"}, {"id": "b", "agentId": "agent-b"}]})
        run_id = created["team"]["id"]
        assignments = {}
        for participant in ("a", "b"):
            _, result = self.call("POST", f"/v1/teams/{run_id}/assignments", body={
                "participantId": participant, "assignment": "research", "purpose": "research"})
            assignments[participant] = next(a["id"] for a in result["team"]["assignments"]
                                             if a["participant_id"] == participant)
        initial = {}
        for participant in ("a", "b"):
            code, result = self.call("POST", f"/v1/teams/{run_id}/dispatch", body={
                "assignmentId": assignments[participant], "requestId": "initial-" + participant})
            self.assertEqual(code, 202, result)
            initial[participant] = result["attempt"]["task_id"]
        for task_id in initial.values():
            self.assertEqual(server._wait_for_terminal(task_id, 5000)["status"], "completed")
        self.call("GET", f"/v1/teams/{run_id}")

        _, addressed = self.call("POST", f"/v1/teams/{run_id}/messages", body={
            "recipientId": "b", "body": "tag b after completion"})
        self.assertTrue(any(item.get("status") == "dispatched" for item in addressed["wake"]), addressed)
        deadline = time.monotonic() + 8
        attempts = []
        while time.monotonic() < deadline:
            attempts = server.TEAM_STORE.list_attempts(run_id)
            if len(attempts) >= 4 and all(item["status"] in ("completed", "failed", "cancelled", "timed_out")
                                          for item in attempts):
                break
            for item in attempts:
                if item.get("task_id"):
                    server._wait_for_terminal(item["task_id"], 200)
            self.call("GET", f"/v1/teams/{run_id}")
            time.sleep(0.05)
        self.assertEqual(len(attempts), 4, attempts)
        self.assertTrue(all(item["status"] == "completed" for item in attempts), attempts)
        run = server.TEAM_STORE.get_run(run_id)
        self.assertIn("peer reply", [event["body"] for event in run["events"]])
        self.assertIn("peer result integrated", [event["body"] for event in run["events"]])
        conclusion = next(event for event in run["events"] if event["kind"] == "conclusion")
        self.assertIn("Peer result verified", conclusion["body"])
        _, lead_inbox = self.call("GET", f"/v1/teams/{run_id}/inbox")
        self.assertIn(conclusion["id"], [event["id"] for event in lead_inbox["events"]])
        self.assertEqual(len([item for item in attempts if item["request_id"].startswith("wake:")]), 2)


if __name__ == "__main__":
    unittest.main()
