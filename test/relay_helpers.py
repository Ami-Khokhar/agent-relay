"""Shared helpers for the agent-relay test suite."""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error as urlerror
from urllib import request as urlrequest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = sys.executable
SERVER = os.path.join(ROOT, "src", "server.py")
MCP_SERVER = os.path.join(ROOT, "src", "mcp_server.py")
FAKE_ADAPTER = os.path.join(ROOT, "test", "fixtures", "fake_stdio_adapter.py")
TOKEN = "relay-test-token"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Relay:
    """Spawns src/server.py with a one-agent registry and talks to it over HTTP."""

    def __init__(self, agent, env=None):
        self.dir = tempfile.mkdtemp(prefix="a2a-relay-")
        self.config = os.path.join(self.dir, "agents.json")
        with open(self.config, "w", encoding="utf-8") as handle:
            json.dump({"agents": [agent]}, handle)
        self.port = free_port()
        child_env = {**os.environ, "A2A_RELAY_PORT": str(self.port), "A2A_AGENTS_FILE": self.config,
                     "AGENT_RELAY_TOKEN": TOKEN}
        child_env.update(env or {})
        self.proc = subprocess.Popen(
            [PYTHON, SERVER], cwd=ROOT, env=child_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.base = f"http://127.0.0.1:{self.port}"
        self._wait_ready()

    def _wait_ready(self):
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.proc.poll() is not None:
                _, stderr = self.proc.communicate()
                raise RuntimeError(f"relay exited {self.proc.returncode}: "
                                   f"{stderr.decode('utf-8', 'replace')}")
            try:
                with urlrequest.urlopen(self.base + "/healthz", timeout=0.5) as response:
                    if response.status == 200:
                        return
            except Exception:  # noqa: BLE001 - retry until the listener is up
                time.sleep(0.02)
        raise RuntimeError("relay did not start")

    def request(self, method, path, body=None, headers=None, token=TOKEN):
        if isinstance(body, str):
            data = body.encode("utf-8")  # raw body, sent as-is
        else:
            data = json.dumps(body).encode("utf-8") if body is not None else None
        base_headers = {"content-type": "application/json"} if data else {}
        if token:
            base_headers["authorization"] = f"Bearer {token}"
        headers = {**base_headers, **(headers or {})}
        request = urlrequest.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urlrequest.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8") or "null")
        except urlerror.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8") or "null")

    def submit(self, **body):
        return self.request("POST", "/v1/tasks", body)

    def wait_task(self, task_id, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, task = self.request("GET", f"/v1/tasks/{task_id}")
            if task.get("status") not in ("queued", "running"):
                return task
            time.sleep(0.02)
        raise RuntimeError("task did not finish")

    def run_task(self, **body):
        _, submitted = self.submit(**body)
        return self.wait_task(submitted["id"])

    def close(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def start_http_server(handler):
    """handler(request) -> None; returns (server, port)."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            handler(self)

        def do_GET(self):
            handler(self)

        def do_DELETE(self):
            handler(self)

        def do_PUT(self):
            handler(self)

        def do_PATCH(self):
            handler(self)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def stop_http_server(server):
    server.shutdown()
    server.server_close()


def send_json(handler_request, status, value):
    body = json.dumps(value).encode("utf-8")
    handler_request.send_response(status)
    handler_request.send_header("content-type", "application/json")
    handler_request.send_header("content-length", str(len(body)))
    handler_request.end_headers()
    handler_request.wfile.write(body)


class McpClient:
    """Spawns src/mcp_server.py and speaks JSON-RPC 2.0 over its stdio."""

    def __init__(self, relay_url, env=None):
        env = {**os.environ, "A2A_RELAY_URL": relay_url, "AGENT_RELAY_TOKEN": TOKEN, **(env or {})}
        self.proc = subprocess.Popen(
            [PYTHON, MCP_SERVER], cwd=ROOT, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.lock = threading.Lock()
        self.pending = {}
        self.next_id = 1
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            with self.lock:
                callback = self.pending.pop(message.get("id"), None)
            if callback is not None:
                callback(message)

    def call(self, method, params=None, timeout=15):
        with self.lock:
            request_id = self.next_id
            self.next_id += 1
            event = threading.Event()
            box = {}
            self.pending[request_id] = lambda message: (box.__setitem__("message", message), event.set())
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        if not event.wait(timeout):
            raise TimeoutError(method)
        return box["message"]

    def call_tool(self, name, arguments, timeout=15):
        message = self.call("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        if "error" in message:
            raise RuntimeError(message["error"]["message"])
        return message["result"]

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()
