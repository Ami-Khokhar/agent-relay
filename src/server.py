#!/usr/bin/env python3
"""agent-relay: a local HTTP task service for delegating work between coding agents.

The service owns task IDs, queueing, status, timeouts, cancellation, idempotency, and a
registry of adapters. See README.md for the HTTP API and the relay.adapter/v1 contract.
"""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ADAPTER_PROTOCOL = "relay.adapter/v1"
INHERITED_ENV = (
    "PATH", "HOME", "USER", "SHELL", "TMPDIR", "LANG", "LC_ALL",
    "SystemRoot", "ComSpec", "PATHEXT",
)
AGENT_ID_RE = re.compile(r"[A-Za-z0-9_.~-]+")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _positive_int(name, fallback):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return fallback
    try:
        value = int(raw)
    except ValueError:
        sys.exit(f"{name} must be a positive integer")
    if value <= 0:
        sys.exit(f"{name} must be a positive integer")
    return value


HOST = os.environ.get("A2A_RELAY_HOST") or "127.0.0.1"
CONFIG_PATH = os.environ.get("A2A_AGENTS_FILE") or str(
    Path(__file__).resolve().parent.parent / "config" / "agents.json"
)
PORT = _positive_int("A2A_RELAY_PORT", 43124)
MAX_BODY = _positive_int("A2A_RELAY_MAX_BODY_BYTES", 1_048_576)
TIMEOUT_MS = _positive_int("A2A_RELAY_TIMEOUT_MS", 120_000)
MAX_OUTPUT = _positive_int("A2A_RELAY_MAX_OUTPUT_BYTES", 262_144)
MAX_TASKS = _positive_int("A2A_RELAY_MAX_TASKS", 1000)
MAX_ACTIVE = _positive_int("A2A_RELAY_MAX_ACTIVE", 4)
MAX_COMMAND_INPUT = _positive_int("A2A_RELAY_MAX_COMMAND_INPUT_BYTES", 65_536)

LOCK = threading.RLock()
AGENTS = {}
TASKS = {}
REQUEST_IDS = {}
QUEUE = []
ACTIVE = 0
SHUTTING_DOWN = False


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def visible(task):
    return {key: value for key, value in task.items() if not key.startswith("_")}


def load_registry(path):
    with open(path, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict) or not isinstance(config.get("agents"), list):
        raise ValueError("config.agents must be an array")
    agents = {}
    for raw in config["agents"]:
        if not isinstance(raw, dict):
            raise ValueError("Invalid agent entry")
        agent_id = raw.get("id")
        agent_type = raw.get("type") or ("command" if raw.get("command") else "http")
        if not isinstance(agent_id, str) or not AGENT_ID_RE.fullmatch(agent_id) or agent_id in agents:
            raise ValueError(f"Invalid or duplicate agent: {agent_id}")
        if agent_type in ("command", "stdio"):
            args = raw.get("args", [])
            if (not isinstance(raw.get("command"), str) or not isinstance(args, list)
                    or not all(isinstance(item, str) for item in args)):
                raise ValueError(f"Invalid {agent_type} agent: {agent_id}")
        inherit = raw.get("inheritEnv")
        if inherit is not None and (not isinstance(inherit, list) or not all(
                isinstance(item, str) and ENV_NAME_RE.fullmatch(item) for item in inherit)):
            raise ValueError(f"Invalid inherited environment: {agent_id}")
        env = raw.get("env")
        if env is not None and (not isinstance(env, dict)
                                or not all(isinstance(item, str) for item in env.values())):
            raise ValueError(f"Invalid agent env: {agent_id}")
        agent_timeout = raw.get("timeoutMs")
        if agent_timeout is not None and (isinstance(agent_timeout, bool)
                                          or not isinstance(agent_timeout, int) or agent_timeout <= 0):
            raise ValueError(f"Invalid agent timeout: {agent_id}")
        if agent_type == "http":
            url = raw.get("url")
            if not isinstance(url, str) or urlsplit(url).scheme not in ("http", "https"):
                raise ValueError(f"Invalid HTTP agent: {agent_id}")
        caps = raw.get("capabilities")
        if caps is not None and (not isinstance(caps, dict)
                                 or not all(isinstance(item, bool) for item in caps.values())):
            raise ValueError(f"Invalid agent capabilities: {agent_id}")
        if isinstance(caps, dict) and (caps.get("newTasks") is False
                                       or caps.get("nativeSessions") is True
                                       or caps.get("streaming") is True):
            raise ValueError(f"Unsupported agent capabilities: {agent_id}")
        if agent_type not in ("command", "stdio", "http"):
            raise ValueError(f"Unknown adapter: {agent_type}")
        merged = {"newTasks": True, "nativeSessions": False, "streaming": False}
        if isinstance(caps, dict):
            merged.update(caps)
        agents[agent_id] = {**raw, "type": agent_type, "args": raw.get("args", []), "capabilities": merged}
    return agents


def environment(agent):
    env = {}
    for key in INHERITED_ENV:
        if key in os.environ:
            env[key] = os.environ[key]
    for key in agent.get("inheritEnv") or []:
        if key in os.environ:
            env[key] = os.environ[key]
    env.update(agent.get("env") or {})
    env["A2A_ADAPTER_PROTOCOL"] = ADAPTER_PROTOCOL
    return env


def adapter_request(task):
    return {
        "protocolVersion": ADAPTER_PROTOCOL,
        "task": {
            "id": task["id"],
            "sessionId": task["sessionId"],
            "input": task["input"],
            "timeoutMs": task["timeoutMs"],
        },
    }


class _Capture:
    """Bounds combined adapter output to a byte budget."""

    def __init__(self, limit):
        self.limit = limit
        self.captured = 0
        self.truncated = False
        self.lock = threading.Lock()

    def feed(self, chunk):
        with self.lock:
            remaining = self.limit - self.captured
            if remaining <= 0:
                self.truncated = True
                return b""
            kept = chunk[:remaining]
            self.captured += len(kept)
            if len(kept) < len(chunk):
                self.truncated = True
            return kept


def _pump(stream, sink, capture):
    def run():
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                kept = capture.feed(chunk)
                if kept:
                    sink.append(kept)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _terminate(proc):
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        pass

    def _kill():
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    killer = threading.Timer(1.0, _kill)
    killer.daemon = True
    killer.start()


def _wait(proc, task):
    try:
        proc.wait(timeout=task["timeoutMs"] / 1000.0)
        return False
    except subprocess.TimeoutExpired:
        _terminate(proc)
        proc.wait()
        return True


def _timed_out(exc, started, task):
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    if isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)):
        return True
    return (time.monotonic() - started) * 1000 >= task["timeoutMs"]


def _run_command(agent, task):
    env = environment(agent)
    env["A2A_TASK_ID"] = task["id"]
    env["A2A_SESSION_ID"] = task["sessionId"]
    try:
        proc = subprocess.Popen(
            [agent["command"], *agent["args"], task["input"]],
            cwd=agent.get("cwd"), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except OSError as exc:
        _finish(task, "failed", error=exc.strerror or str(exc))
        return
    with LOCK:
        task["_proc"] = proc
    capture = _Capture(MAX_OUTPUT)
    out, err = [], []
    readers = (_pump(proc.stdout, out, capture), _pump(proc.stderr, err, capture))
    timed_out = _wait(proc, task)
    for reader in readers:
        reader.join()
    output = b"".join(out).decode("utf-8", "replace").strip()
    error = b"".join(err).decode("utf-8", "replace").strip()
    if timed_out:
        _finish(task, "timed_out", error=f"Agent exceeded {task['timeoutMs']} ms")
    elif proc.returncode == 0:
        _finish(task, "completed", output=output, outputTruncated=capture.truncated)
    else:
        _finish(task, "failed", output=output,
                error=error or f"Agent exited with {proc.returncode}",
                outputTruncated=capture.truncated)


def _run_stdio(agent, task):
    env = environment(agent)
    try:
        proc = subprocess.Popen(
            [agent["command"], *agent["args"]],
            cwd=agent.get("cwd"), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except OSError as exc:
        _finish(task, "failed", error=exc.strerror or str(exc))
        return
    with LOCK:
        task["_proc"] = proc
    capture = _Capture(MAX_OUTPUT)
    out, err = [], []
    readers = (_pump(proc.stdout, out, capture), _pump(proc.stderr, err, capture))
    stdin_error = None
    try:
        proc.stdin.write((json.dumps(adapter_request(task)) + "\n").encode("utf-8"))
        proc.stdin.close()
    except (BrokenPipeError, OSError) as exc:
        stdin_error = exc.strerror or str(exc)
    timed_out = _wait(proc, task)
    for reader in readers:
        reader.join()
    stdout = b"".join(out).decode("utf-8", "replace")
    stderr = b"".join(err).decode("utf-8", "replace")
    if timed_out:
        _finish(task, "timed_out", error=f"Agent exceeded {task['timeoutMs']} ms")
        return
    if proc.returncode != 0:
        _finish(task, "failed", error=stderr.strip() or f"Adapter exited with {proc.returncode}",
                outputTruncated=capture.truncated)
        return
    if capture.truncated:
        _finish(task, "failed", error="Adapter response exceeded output limit", outputTruncated=True)
        return
    try:
        response = json.loads(stdout)
    except ValueError:
        _finish(task, "failed",
                error=f"Adapter stdin failed: {stdin_error}" if stdin_error else "Adapter returned invalid JSON")
        return
    if (not isinstance(response, dict) or response.get("protocolVersion") != ADAPTER_PROTOCOL
            or response.get("status") not in ("completed", "failed")):
        _finish(task, "failed", error="Adapter returned an invalid relay.adapter/v1 response")
        return
    if "output" in response and not isinstance(response["output"], str):
        _finish(task, "failed", error="Adapter response output must be a string")
        return
    if "error" in response and not isinstance(response["error"], str):
        _finish(task, "failed", error="Adapter response error must be a string")
        return
    if response["status"] == "failed" and not response.get("error"):
        _finish(task, "failed", error="Adapter reported failure without an error")
        return
    _finish(task, response["status"], output=response.get("output", ""), error=response.get("error"))


def _run_http(agent, task):
    from urllib import error as urlerror
    from urllib import request as urlrequest

    payload = json.dumps(adapter_request(task)).encode("utf-8")
    request = urlrequest.Request(agent["url"], data=payload, method="POST",
                                 headers={"content-type": "application/json"})
    started = time.monotonic()
    try:
        with urlrequest.urlopen(request, timeout=task["timeoutMs"] / 1000.0) as response:
            content_type = (response.headers.get("content-type") or "").lower()
            raw = response.read(MAX_OUTPUT + 1)
            code = response.status
            ok = 200 <= code < 300
    except urlerror.HTTPError as exc:
        content_type = ((exc.headers.get("content-type") if exc.headers else "") or "").lower()
        raw = exc.read(MAX_OUTPUT + 1)
        code = exc.code
        ok = False
    except (urlerror.URLError, socket.timeout, TimeoutError) as exc:
        status = "timed_out" if _timed_out(exc, started, task) else "failed"
        _finish(task, status, error=str(getattr(exc, "reason", exc)))
        return
    truncated = len(raw) > MAX_OUTPUT
    text = raw[:MAX_OUTPUT].decode("utf-8", "replace")
    is_json = "application/json" in content_type
    if truncated:
        if is_json:
            _finish(task, "failed", error="Adapter response exceeded output limit", outputTruncated=True)
        else:
            _finish(task, "completed" if ok else "failed", output=text,
                    error=None if ok else f"HTTP agent returned {code}", outputTruncated=True)
        return
    if not is_json:
        _finish(task, "completed" if ok else "failed", output=text,
                error=None if ok else f"HTTP agent returned {code}")
        return
    try:
        value = json.loads(text)
    except ValueError:
        _finish(task, "failed", error="HTTP adapter returned invalid JSON")
        return
    if (not isinstance(value, dict) or value.get("protocolVersion") != ADAPTER_PROTOCOL
            or value.get("status") not in ("completed", "failed")):
        _finish(task, "failed", error="HTTP adapter returned an invalid relay.adapter/v1 response")
        return
    if "output" in value and not isinstance(value["output"], str):
        _finish(task, "failed", error="Adapter response output must be a string")
        return
    if "error" in value and not isinstance(value["error"], str):
        _finish(task, "failed", error="Adapter response error must be a string")
        return
    if value["status"] == "failed" and not value.get("error"):
        _finish(task, "failed", error="Adapter reported failure without an error")
        return
    if not ok:
        _finish(task, "failed", error=value.get("error") or f"HTTP agent returned {code}",
                output=value.get("output", ""))
        return
    _finish(task, value["status"], output=value.get("output", ""), error=value.get("error"))


def _worker(agent, task):
    try:
        kind = agent["type"]
        if kind == "command":
            _run_command(agent, task)
        elif kind == "stdio":
            _run_stdio(agent, task)
        else:
            _run_http(agent, task)
    except Exception as exc:  # noqa: BLE001 - surface any adapter failure on the task
        _finish(task, "failed", error=str(exc))


def _finish(task, status, **fields):
    global ACTIVE
    with LOCK:
        if task["status"] not in ("queued", "running"):
            return
        task["status"] = status
        task["finishedAt"] = now_iso()
        task.update({key: value for key, value in fields.items() if value is not None})
        task.pop("_proc", None)
        if task.pop("_counted", False):
            ACTIVE -= 1
    _drain()


def _drain():
    global ACTIVE
    with LOCK:
        while ACTIVE < MAX_ACTIVE and QUEUE:
            task = QUEUE.pop(0)
            if task["status"] != "queued":
                continue
            agent = AGENTS.get(task["agentId"])
            if agent is None:
                task["status"] = "failed"
                task["finishedAt"] = now_iso()
                task["error"] = "unknown_agent"
                continue
            ACTIVE += 1
            task["_counted"] = True
            task["status"] = "running"
            task["startedAt"] = now_iso()
            threading.Thread(target=_worker, args=(agent, task), daemon=True).start()


def _make_room():
    if len(TASKS) < MAX_TASKS:
        return True
    terminal = next((task for task in TASKS.values()
                     if task["status"] not in ("queued", "running")), None)
    if terminal is None:
        return False
    del TASKS[terminal["id"]]
    request_key = terminal.get("_request_key")
    if request_key:
        REQUEST_IDS.pop(request_key, None)
    return True


def _terminate_task(task):
    proc = task.get("_proc")
    if proc is not None:
        _terminate(proc)


def _valid(value, limit=128):
    return isinstance(value, str) and 0 < len(value) <= limit


def _is_positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _agent_listing():
    listing = []
    for agent in AGENTS.values():
        entry = {
            "id": agent["id"],
            "name": agent.get("name") or agent["id"],
            "adapter": agent["type"],
            "capabilities": {
                **agent["capabilities"],
                "cancellation": "request_only" if agent["type"] == "http" else "process_signal",
            },
        }
        if agent.get("description") is not None:
            entry["description"] = agent["description"]
        listing.append(entry)
    return listing


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "agent-relay"

    def log_message(self, *args):  # silence per-request logging
        pass

    def _json(self, status, value):
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PUT(self):
        self._handle("PUT")

    def do_PATCH(self):
        self._handle("PATCH")

    def _handle(self, method):
        try:
            path = urlsplit(self.path).path
            if method == "GET" and path == "/healthz":
                return self._json(200, {"ok": True, "agents": len(AGENTS), "tasks": len(TASKS)})
            if method == "GET" and path == "/v1/agents":
                return self._json(200, {"agents": _agent_listing()})
            if method == "POST" and path == "/v1/tasks":
                return self._submit()
            match = re.fullmatch(r"/v1/tasks/([^/]+)", path)
            if match:
                task = TASKS.get(match.group(1))
                if method == "GET":
                    return self._json(200, visible(task)) if task else self._json(404, {"error": "unknown_task"})
                if method == "DELETE":
                    return self._cancel(task)
            if match or path in ("/v1/tasks", "/v1/agents", "/healthz"):
                return self._json(405, {"error": "method_not_allowed"})
            return self._json(404, {"error": "not_found"})
        except Exception as exc:  # noqa: BLE001 - never leak a stack trace to the client
            self._json(500, {"error": "request_failed", "message": str(exc)})

    def _submit(self):
        global ACTIVE
        length_header = self.headers.get("content-length")
        length = int(length_header) if length_header and length_header.isdigit() else 0
        if length > MAX_BODY:
            self.close_connection = True
            return self._json(413, {"error": "request_failed", "message": "body too large"})
        raw = self.rfile.read(length) if length else b""
        try:
            request = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return self._json(400, {"error": "invalid_json"})
        if not isinstance(request, dict):
            return self._json(400, {"error": "invalid_request"})
        agent = AGENTS.get(request.get("agentId"))
        if agent is None:
            return self._json(404, {"error": "unknown_agent"})
        input_text = request.get("input")
        if not _valid(input_text, MAX_BODY):
            return self._json(400, {"error": "input_required"})
        session_id = request.get("sessionId")
        if session_id is not None and not _valid(session_id):
            return self._json(400, {"error": "invalid_session_id"})
        request_id = request.get("requestId")
        if request_id is not None and not _valid(request_id):
            return self._json(400, {"error": "invalid_request_id"})
        timeout_ms = request.get("timeoutMs")
        if timeout_ms is not None and not _is_positive_int(timeout_ms):
            return self._json(400, {"error": "invalid_timeout"})
        if agent["type"] == "command" and len(input_text.encode("utf-8")) > MAX_COMMAND_INPUT:
            return self._json(413, {"error": "command_input_too_large"})
        request_key = None if request_id is None else f"{agent['id']}\0{request_id}"
        with LOCK:
            prior = REQUEST_IDS.get(request_key) if request_key else None
            if prior is not None:
                if (prior["input"] != input_text
                        or prior.get("_requested_session_id") != session_id
                        or prior.get("_requested_timeout_ms") != timeout_ms):
                    return self._json(409, {"error": "idempotency_conflict"})
                return self._json(200, visible(prior))
            if not _make_room():
                return self._json(503, {"error": "task_capacity_reached"})
            task_timeout = min(timeout_ms or agent.get("timeoutMs") or TIMEOUT_MS, TIMEOUT_MS)
            task = {
                "id": str(uuid.uuid4()),
                "sessionId": session_id or str(uuid.uuid4()),
                "agentId": agent["id"],
                "input": input_text,
                "status": "queued",
                "createdAt": now_iso(),
                "timeoutMs": task_timeout,
            }
            if request_key:
                task["requestId"] = request_id
                task["_request_key"] = request_key
                task["_requested_session_id"] = session_id
                task["_requested_timeout_ms"] = timeout_ms
                REQUEST_IDS[request_key] = task
            TASKS[task["id"]] = task
            QUEUE.append(task)
        self._json(202, visible(task))
        _drain()

    def _cancel(self, task):
        global ACTIVE
        if task is None:
            return self._json(404, {"error": "unknown_task"})
        with LOCK:
            if task["status"] in ("queued", "running"):
                if task["status"] == "running":
                    _terminate_task(task)
                    if task.pop("_counted", False):
                        ACTIVE -= 1
                else:
                    try:
                        QUEUE.remove(task)
                    except ValueError:
                        pass
                task["status"] = "cancelled"
                task["finishedAt"] = now_iso()
                task.pop("_proc", None)
        self._json(200, visible(task))
        _drain()


def _shutdown(signum, _frame):
    global SHUTTING_DOWN
    if SHUTTING_DOWN:
        return
    SHUTTING_DOWN = True
    with LOCK:
        running = [task for task in TASKS.values() if task["status"] == "running"]
    if running:
        name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        print(f"Received {name}; cancelling {len(running)} running task(s)", flush=True)
    for task in running:
        _terminate_task(task)
    time.sleep(1.5 if running else 0)
    os._exit(0)


def main():
    global AGENTS
    try:
        AGENTS = load_registry(CONFIG_PATH)
    except (OSError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"A2A relay listening at http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
