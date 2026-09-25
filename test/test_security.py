"""Local trust boundary tests: API token, browser origins, bind address, HTTP adapter trust."""
from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from relay_helpers import (PYTHON, ROOT, SERVER, TOKEN, McpClient, Relay, free_port, send_json,
                           start_http_server, stop_http_server)
import mcp_server
import server


def _exchange_until_close(test, port, request):
    """Send raw bytes and read until the server closes; fail if it keeps the connection open."""
    data = b""
    with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
        sock.sendall(request)
        try:
            for chunk in iter(lambda: sock.recv(65536), b""):
                data += chunk
        except socket.timeout:
            test.fail("connection stayed open after an error response")
    return data


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

    @unittest.skipUnless(os.name == "posix", "file modes are POSIX-only")
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
            self.assertEqual(relay.request("GET", "/v1/agents", token=TOKEN)[0], 401)
            status, agents = relay.request("GET", "/v1/agents", token=token)
            self.assertEqual(status, 200)
            self.assertNotIn(token, json.dumps(agents))
        finally:
            relay.close()

        with open(token_file, "w", encoding="utf-8") as handle:
            handle.write("shared-token\n")
        os.chmod(token_file, 0o644)
        with self.assertRaises(RuntimeError) as raised:
            Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print(1)"]}, env=env)
        self.assertIn("accessible to other users", str(raised.exception))
        self.assertNotIn("shared-token", str(raised.exception))

    def test_an_unset_token_never_authorizes(self):
        self.assertEqual(server.TOKEN, "")
        for header in ("Bearer ", "Bearer", "bearer  "):
            fake = types.SimpleNamespace(headers={"authorization": header})
            self.assertFalse(server.Handler._authorized(fake), header)

    def test_rejected_requests_close_the_connection_instead_of_parsing_the_body(self):
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print(1)"]})
        try:
            smuggled = b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n"
            request = (b"POST /v1/tasks HTTP/1.1\r\nHost: x\r\ncontent-type: application/json\r\n"
                       b"content-length: %d\r\n\r\n" % len(smuggled)) + smuggled
            data = _exchange_until_close(self, relay.port, request)
            self.assertIn(b" 401 ", data)
            self.assertEqual(data.count(b"HTTP/1.1 "), 1, data)
        finally:
            relay.close()

    def test_mcp_interface_reads_the_token_file_the_relay_created(self):
        directory = tempfile.mkdtemp(prefix="a2a-mcp-token-")
        self.addCleanup(shutil.rmtree, directory, True)
        env = {"AGENT_RELAY_TOKEN": "", "AGENT_RELAY_TOKEN_FILE": os.path.join(directory, "token")}
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print(1)"]}, env=env)
        client = McpClient(relay.base, env={**env, "A2A_RELAY_AUTOSTART": "0"})
        try:
            result = client.call_tool("list_agents", {})
            self.assertFalse(result.get("isError"), result)
            self.assertEqual(result["structuredContent"]["agents"][0]["id"], "echo")
        finally:
            client.close()
            relay.close()


    def test_listed_origins_are_accepted_and_others_refused(self):
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print(1)"]},
                      env={"AGENT_RELAY_ALLOWED_ORIGINS": "https://tool.example, https://other.example"})
        try:
            for origin in ("https://tool.example", "https://other.example"):
                self.assertEqual(relay.request("GET", "/v1/agents", headers={"origin": origin})[0], 200)
            status, payload = relay.request("GET", "/v1/agents", headers={"origin": "https://evil.example"})
            self.assertEqual((status, payload["error"]), (403, "origin_not_allowed"))
        finally:
            relay.close()

    def test_method_errors_close_the_connection_too(self):
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print(1)"]})
        try:
            smuggled = b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n"
            request = (b"PUT /v1/tasks HTTP/1.1\r\nHost: x\r\nauthorization: Bearer %s\r\n"
                       b"content-length: %d\r\n\r\n" % (TOKEN.encode(), len(smuggled))) + smuggled
            data = _exchange_until_close(self, relay.port, request)
            self.assertIn(b" 405 ", data)
            self.assertIn(b"connection: close", data.lower())
            self.assertEqual(data.count(b"HTTP/1.1 "), 1, data)
        finally:
            relay.close()

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "needs O_NOFOLLOW")
    def test_refuses_a_symlinked_or_empty_token_file(self):
        directory = tempfile.mkdtemp(prefix="a2a-token-link-")
        self.addCleanup(shutil.rmtree, directory, True)
        real, link = os.path.join(directory, "real"), os.path.join(directory, "token")
        with open(real, "w", encoding="utf-8") as handle:
            handle.write("linked-token\n")
        os.chmod(real, 0o600)
        os.symlink(real, link)
        env = {"AGENT_RELAY_TOKEN_FILE": link, "AGENT_RELAY_TOKEN": ""}
        with self.assertRaises(RuntimeError) as raised:
            Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print(1)"]}, env=env)
        self.assertIn("is a symlink", str(raised.exception))

        os.remove(link)
        open(link, "w").close()
        os.chmod(link, 0o600)
        with self.assertRaises(RuntimeError) as raised:
            Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print(1)"]}, env=env)
        self.assertIn("delete it to generate a new token", str(raised.exception))

    def test_mcp_client_keeps_the_token_away_from_redirects_and_cleartext(self):
        hits = []
        def elsewhere(request):
            hits.append(request.headers.get("authorization"))
            send_json(request, 200, {"agents": []})

        target, target_port = start_http_server(elsewhere)
        def redirector(request):
            request.send_response(302)
            request.send_header("location", f"http://127.0.0.1:{target_port}/v1/agents")
            request.send_header("content-length", "0")
            request.end_headers()
        origin, origin_port = start_http_server(redirector)
        try:
            with mock.patch.dict(os.environ, {"AGENT_RELAY_TOKEN": "secret-token"}):
                with self.assertRaises(mcp_server.RelayError) as raised:
                    mcp_server._http_request(f"http://127.0.0.1:{origin_port}", "/v1/agents")
                self.assertEqual(raised.exception.status, 302)
                self.assertEqual(hits, [])
                with self.assertRaises(mcp_server.RelayError) as raised:
                    mcp_server._http_request("http://relay.example:43124", "/v1/agents", timeout=1)
                self.assertEqual(raised.exception.data["error"], "insecure_relay_url")
                self.assertTrue(mcp_server._loopback("http://127.0.0.2:43124"))
        finally:
            stop_http_server(origin)
            stop_http_server(target)


    def test_reload_accepts_any_loopback_client_address(self):
        relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print(1)"]})
        try:
            request = (b"POST /v1/admin/reload HTTP/1.1\r\nHost: x\r\nauthorization: Bearer %s\r\n"
                       b"content-length: 0\r\n\r\n" % TOKEN.encode())
            with socket.create_connection(("127.0.0.1", relay.port), timeout=3,
                                          source_address=("127.0.0.2", 0)) as sock:
                sock.sendall(request)
                self.assertIn(b" 200 ", sock.recv(65536))
        finally:
            relay.close()

    def test_smoke_script_uses_the_token_and_rejects_unsafe_ones(self):
        relay = Relay({"id": "pong", "command": PYTHON, "args": ["-c", "print('PONG')"]})
        try:
            smoke = os.path.join(ROOT, "scripts", "smoke.sh")
            env = {**os.environ, "AGENT_RELAY_URL": relay.base, "AGENT_RELAY_TOKEN": TOKEN}
            done = subprocess.run(["bash", smoke, "pong", "5000"], env=env, capture_output=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn(b"status:   completed", done.stdout)
            for bad in ('x"y', "abc\nurl = \"http://evil.example\""):
                done = subprocess.run(["bash", smoke, "pong", "5000"], capture_output=True, timeout=30,
                                      env={**env, "AGENT_RELAY_TOKEN": bad})
                self.assertEqual(done.returncode, 1)
                self.assertIn(b"may only contain letters, digits", done.stderr)
        finally:
            relay.close()


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
