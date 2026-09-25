"""Local trust boundary tests: API token, browser origins, bind address, HTTP adapter trust."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from urllib import request as urlrequest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from relay_helpers import (PYTHON, ROOT, SERVER, TOKEN, Relay, free_port, send_json,
                           start_http_server, stop_http_server)
import server


class ApiTokenTests(unittest.TestCase):
    def test_every_task_route_requires_the_token(self):
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print(1)"]})
        try:
            routes = [("GET", "/v1/agents", None), ("GET", "/v1/tasks", None),
                      ("POST", "/v1/tasks", {"agentId": "echo", "input": "x"}),
                      ("GET", "/v1/tasks/some-id", None), ("DELETE", "/v1/tasks/some-id", None),
                      ("POST", "/v1/admin/reload", None)]
            for token in (None, "wrong-token"):
                for method, path, body in routes:
                    status, payload = relay.request(method, path, body, token=token)
                    self.assertEqual(status, 401, f"{method} {path} with token {token!r}")
                    self.assertEqual(payload["error"], "unauthorized")
            self.assertEqual(relay.request("GET", "/v1/tasks")[1], {"tasks": []})
            status, health = relay.request("GET", "/healthz", token=None)
            self.assertEqual(status, 200)
            self.assertNotIn(TOKEN, json.dumps(health))
        finally:
            relay.close()

    def test_simple_cross_origin_post_is_rejected_and_the_agent_does_not_run(self):
        directory = tempfile.mkdtemp(prefix="a2a-origin-")
        self.addCleanup(shutil.rmtree, directory, True)
        marker = os.path.join(directory, "ran")
        relay = Relay({"id": "probe", "command": PYTHON,
                       "args": ["-c", "import os; open(os.environ['MARKER'], 'w').write('x')"],
                       "env": {"MARKER": marker}})
        try:
            body = json.dumps({"agentId": "probe", "input": "x"})
            browser = {"content-type": "text/plain", "origin": "https://example.invalid"}
            for token in (None, TOKEN):
                status, payload = relay.request("POST", "/v1/tasks", body, headers=browser, token=token)
                self.assertEqual(status, 403)
                self.assertEqual(payload["error"], "origin_not_allowed")
            status, payload = relay.request("POST", "/v1/tasks", body,
                                            headers={"content-type": "text/plain"})
            self.assertEqual(status, 415)
            self.assertEqual(payload["error"], "unsupported_media_type")
            time.sleep(0.3)
            self.assertFalse(os.path.exists(marker))
            self.assertEqual(relay.request("GET", "/v1/tasks")[1], {"tasks": []})
        finally:
            relay.close()

    def test_creates_a_private_token_file_and_refuses_a_shared_one(self):
        directory = tempfile.mkdtemp(prefix="a2a-token-")
        self.addCleanup(shutil.rmtree, directory, True)
        token_file = os.path.join(directory, "sub", "token")
        env = {"AGENT_RELAY_TOKEN_FILE": token_file, "AGENT_RELAY_TOKEN": ""}
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print(1)"]}, env=env)
        try:
            self.assertEqual(stat.S_IMODE(os.stat(token_file).st_mode), 0o600)
            with open(token_file, encoding="utf-8") as handle:
                token = handle.read().strip()
            self.assertGreaterEqual(len(token), 32)
            self.assertEqual(relay.request("GET", "/v1/agents", token=token)[0], 200)
            self.assertEqual(relay.request("GET", "/v1/agents", token=TOKEN)[0], 401)
            _, agents = relay.request("GET", "/v1/agents", token=token)
            self.assertNotIn(token, json.dumps(agents))
        finally:
            relay.close()

        os.makedirs(os.path.dirname(token_file), exist_ok=True)
        with open(token_file, "w", encoding="utf-8") as handle:
            handle.write("shared-token\n")
        os.chmod(token_file, 0o644)
        with self.assertRaises(RuntimeError) as raised:
            Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print(1)"]}, env=env)
        self.assertIn("accessible to other users", str(raised.exception))
        self.assertNotIn("shared-token", str(raised.exception))


class NetworkBoundaryTests(unittest.TestCase):
    def _start(self, host, extra=None):
        directory = tempfile.mkdtemp(prefix="a2a-bind-")
        self.addCleanup(shutil.rmtree, directory, True)
        config = os.path.join(directory, "agents.json")
        with open(config, "w", encoding="utf-8") as handle:
            json.dump({"agents": []}, handle)
        env = {**os.environ, "A2A_AGENTS_FILE": config, "AGENT_RELAY_HOST": host,
               "AGENT_RELAY_PORT": str(free_port()), "AGENT_RELAY_TOKEN": TOKEN, **(extra or {})}
        return subprocess.Popen([PYTHON, SERVER], cwd=ROOT, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_refuses_a_non_loopback_bind_without_the_explicit_opt_in(self):
        proc = self._start("0.0.0.0")
        _, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refusing to listen on non-loopback address 0.0.0.0", stderr.decode())

        opted_in = self._start("0.0.0.0", {"AGENT_RELAY_UNSAFE_ALLOW_NON_LOOPBACK": "1"})
        try:
            line = opted_in.stderr.readline().decode()
            self.assertIn("WARNING: listening on non-loopback address", line)
        finally:
            opted_in.kill()
            opted_in.communicate(timeout=10)

    def test_rejects_cleartext_http_adapters_on_non_loopback_hosts(self):
        directory = tempfile.mkdtemp(prefix="a2a-cleartext-")
        self.addCleanup(shutil.rmtree, directory, True)
        config = os.path.join(directory, "agents.json")

        def load(agent):
            with open(config, "w", encoding="utf-8") as handle:
                json.dump({"agents": [agent]}, handle)
            return server.load_registry(config)

        with self.assertRaisesRegex(ValueError, "cleartext"):
            load({"id": "remote", "type": "http", "url": "http://agents.example.com/run"})
        self.assertIn("remote", load({"id": "remote", "type": "http",
                                      "url": "http://agents.example.com/run", "allowInsecureHttp": True}))
        self.assertIn("remote", load({"id": "remote", "type": "http", "url": "https://agents.example.com/run"}))
        self.assertIn("local", load({"id": "local", "type": "http", "url": "http://127.0.0.1:9000/run"}))

    def test_does_not_follow_http_adapter_redirects(self):
        hits = []

        def elsewhere(request):
            hits.append(request.path)
            send_json(request, 200, {"protocolVersion": "relay.adapter/v1",
                                     "status": "completed", "output": "leaked"})

        target, target_port = start_http_server(elsewhere)

        def redirector(request):
            request.rfile.read(int(request.headers.get("content-length", 0)))
            request.send_response(307)
            request.send_header("location", f"http://127.0.0.1:{target_port}/steal")
            request.send_header("content-length", "0")
            request.end_headers()

        origin, origin_port = start_http_server(redirector)
        relay = Relay({"id": "hosted", "type": "http", "url": f"http://127.0.0.1:{origin_port}/run"})
        try:
            task = relay.run_task(agentId="hosted", input="secret prompt")
            self.assertEqual(task["status"], "failed")
            self.assertIn("redirects are not followed", task["error"])
            self.assertEqual(hits, [])
        finally:
            relay.close()
            stop_http_server(origin)
            stop_http_server(target)


if __name__ == "__main__":
    unittest.main()
