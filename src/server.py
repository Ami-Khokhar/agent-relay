#!/usr/bin/env python3
"""agent-relay: a local HTTP task service for delegating work between coding agents.

The service owns task IDs, queueing, status, timeouts, cancellation, idempotency, and a
registry of adapters. See README.md for the HTTP API and the relay.adapter/v1 contract.
"""
from __future__ import annotations

import errno
import hmac
import ipaddress
import json
import os
import re
import secrets
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
from urllib import request as urlrequest
from urllib.parse import parse_qs, urlsplit

ADAPTER_PROTOCOL = "relay.adapter/v1"
INHERITED_ENV = (
    "PATH", "HOME", "USER", "SHELL", "TMPDIR", "LANG", "LC_ALL",
    "SystemRoot", "ComSpec", "PATHEXT",
)
AGENT_ID_RE = re.compile(r"[A-Za-z0-9_.~-]+")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
TASK_FIELDS = {"agentId", "input", "sessionId", "requestId", "timeoutMs", "cwd"}
STATUSES = {"queued", "running", "completed", "failed", "timed_out", "cancelled"}


def _env(*names, default=None):
    """Return the first non-empty environment value among ``names``.

    The canonical prefix is AGENT_RELAY_; the historical A2A_RELAY_ names are kept as
    aliases so existing setups keep working.
    """
    for name in names:
        value = os.environ.get(name)
        if value is not None and value != "":
            return value
    return default


def _positive_int(default, *names):
    raw = _env(*names)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        sys.exit(f"{names[0]} must be a positive integer")
    if value <= 0:
        sys.exit(f"{names[0]} must be a positive integer")
    return value


def _non_negative_int(default, *names):
    raw = _env(*names)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        sys.exit(f"{names[0]} must be a non-negative integer")
    if value < 0:
        sys.exit(f"{names[0]} must be a non-negative integer")
    return value


HOST = _env("AGENT_RELAY_HOST", "A2A_RELAY_HOST", default="127.0.0.1")
PORT = _positive_int(43124, "AGENT_RELAY_PORT", "A2A_RELAY_PORT")
MAX_BODY = _positive_int(1_048_576, "AGENT_RELAY_MAX_BODY_BYTES", "A2A_RELAY_MAX_BODY_BYTES")
# 15 minutes: real coding tasks routinely run for several minutes.
TIMEOUT_MS = _positive_int(900_000, "AGENT_RELAY_TIMEOUT_MS", "A2A_RELAY_TIMEOUT_MS")
# 0 disables the hard cap; a positive value rejects larger per-agent/request timeouts.
MAX_TIMEOUT_MS = _non_negative_int(0, "AGENT_RELAY_MAX_TIMEOUT_MS", "A2A_RELAY_MAX_TIMEOUT_MS")
MAX_WAIT_MS = _positive_int(600_000, "AGENT_RELAY_MAX_WAIT_MS", "A2A_RELAY_MAX_WAIT_MS")
MAX_OUTPUT = _positive_int(262_144, "AGENT_RELAY_MAX_OUTPUT_BYTES", "A2A_RELAY_MAX_OUTPUT_BYTES")
MAX_TASKS = _positive_int(1000, "AGENT_RELAY_MAX_TASKS", "A2A_RELAY_MAX_TASKS")
MAX_ACTIVE = _positive_int(4, "AGENT_RELAY_MAX_ACTIVE", "A2A_RELAY_MAX_ACTIVE")
MAX_COMMAND_INPUT = _positive_int(65_536, "AGENT_RELAY_MAX_COMMAND_INPUT_BYTES",
                                  "A2A_RELAY_MAX_COMMAND_INPUT_BYTES")
EXPLICIT_CONFIG = _env("AGENT_RELAY_AGENTS_FILE", "A2A_AGENTS_FILE")
# Listening beyond loopback exposes an unencrypted API; it needs a conspicuous opt-in.
ALLOW_NON_LOOPBACK = _env("AGENT_RELAY_UNSAFE_ALLOW_NON_LOOPBACK",
                          "A2A_RELAY_UNSAFE_ALLOW_NON_LOOPBACK") == "1"
ALLOWED_ORIGINS = {origin.strip() for origin in (
    _env("AGENT_RELAY_ALLOWED_ORIGINS", "A2A_RELAY_ALLOWED_ORIGINS", default="").split(","))
    if origin.strip()}
TOKEN = ""

LOCK = threading.RLock()
REGISTRY_PATH = ""
TASK_CONDITION = threading.Condition(LOCK)
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


def default_config_path():
    """Prefer a per-user registry, then the one inside the checkout."""
    user = Path.home() / ".config" / "agent-relay" / "agents.json"
    repo = Path(__file__).resolve().parent.parent / "config" / "agents.json"
    return user if user.exists() else repo


def config_path():
    if EXPLICIT_CONFIG:
        return Path(EXPLICIT_CONFIG)
    return default_config_path()


def token_path():
    explicit = _env("AGENT_RELAY_TOKEN_FILE", "A2A_RELAY_TOKEN_FILE")
    return Path(explicit) if explicit else Path.home() / ".config" / "agent-relay" / "token"


def load_token():
    """Return the API credential: AGENT_RELAY_TOKEN, else the token file (created 0600 if absent).

    Raises ValueError when the token file is readable by other users or is empty.
    """
    value = _env("AGENT_RELAY_TOKEN", "A2A_RELAY_TOKEN")
    if value:
        return value
    path = token_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secrets.token_urlsafe(32) + "\n")
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise ValueError(f"token file {path} is accessible to other users; run: chmod 600 {path}")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"token file {path} is empty")
    return value


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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
        if agent_timeout is not None and MAX_TIMEOUT_MS and agent_timeout > MAX_TIMEOUT_MS:
            raise ValueError(
                f"agent {agent_id} timeoutMs exceeds A2A_RELAY_MAX_TIMEOUT_MS ({MAX_TIMEOUT_MS})")
        cwd = raw.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise ValueError(f"Invalid agent cwd: {agent_id}")
        allowed_roots = raw.get("allowedRoots")
        if allowed_roots is not None and (not isinstance(allowed_roots, list)
                                          or not all(isinstance(item, str) and item
                                                     for item in allowed_roots)):
            raise ValueError(f"Invalid allowedRoots: {agent_id}")
        if agent_type == "http":
            url = raw.get("url")
            if not isinstance(url, str) or urlsplit(url).scheme not in ("http", "https"):
                raise ValueError(f"Invalid HTTP agent: {agent_id}")
            if (urlsplit(url).scheme == "http" and not is_loopback(urlsplit(url).hostname or "")
                    and raw.get("allowInsecureHttp") is not True):
                raise ValueError(
                    f"HTTP agent {agent_id} would send prompts in cleartext to a non-loopback host; "
                    "use https or set \"allowInsecureHttp\": true")
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


def reload_registry():
    """Reload the registry from disk. Raises on an invalid file; callers keep the old one."""
    global AGENTS, REGISTRY_PATH
    path = config_path()
    agents = load_registry(str(path))
    with LOCK:
        AGENTS = agents
        REGISTRY_PATH = str(path)
    return path


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


def _task_cwd(agent, task):
    return task.get("cwd") or agent.get("cwd")


def _run_command(agent, task):
    env = environment(agent)
    env["A2A_TASK_ID"] = task["id"]
    env["A2A_SESSION_ID"] = task["sessionId"]
    try:
        proc = subprocess.Popen(
            [agent["command"], *agent["args"], task["input"]],
            cwd=_task_cwd(agent, task), env=env,
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
            cwd=_task_cwd(agent, task), env=env,
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


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    """Never follow adapter redirects: the prompt must only reach the configured URL."""

    def redirect_request(self, *args, **kwargs):
        return None


def _run_http(agent, task):
    from urllib import error as urlerror

    opener = urlrequest.build_opener(_NoRedirect)
    payload = json.dumps(adapter_request(task)).encode("utf-8")
    request = urlrequest.Request(agent["url"], data=payload, method="POST",
                                 headers={"content-type": "application/json"})
    started = time.monotonic()
    try:
        with opener.open(request, timeout=task["timeoutMs"] / 1000.0) as response:
            content_type = (response.headers.get("content-type") or "").lower()
            raw = response.read(MAX_OUTPUT + 1)
            code = response.status
            ok = 200 <= code < 300
    except urlerror.HTTPError as exc:
        if 300 <= exc.code < 400:
            location = exc.headers.get("location") if exc.headers else None
            _finish(task, "failed", error=f"HTTP adapter redirected ({exc.code} to {location}); "
                                          "redirects are not followed")
            return
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
        TASK_CONDITION.notify_all()
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
                TASK_CONDITION.notify_all()
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
    TASK_CONDITION.notify_all()
    return True


def _terminate_task(task):
    proc = task.get("_proc")
    if proc is not None:
        _terminate(proc)


def _valid(value, limit=128):
    return isinstance(value, str) and 0 < len(value) <= limit


def _is_positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _within(root, candidate):
    try:
        return os.path.commonpath([root, candidate]) == root
    except ValueError:
        return False


def _resolve_task_cwd(agent, requested):
    """Return (cwd, error). A None cwd means the agent's default applies."""
    if requested is None:
        return None, None
    if not isinstance(requested, str) or not requested:
        return None, "invalid_cwd"
    candidate = os.path.realpath(requested)
    roots = list(agent.get("allowedRoots") or [])
    if agent.get("cwd"):
        roots.append(agent["cwd"])
    if not any(_within(os.path.realpath(root), candidate) for root in roots):
        return None, "cwd_not_allowed"
    if not os.path.isdir(candidate):
        return None, "invalid_cwd"
    return candidate, None


def _limits():
    return {
        "timeoutMs": TIMEOUT_MS,
        "maxTimeoutMs": MAX_TIMEOUT_MS or None,
        "maxWaitMs": MAX_WAIT_MS,
        "maxBodyBytes": MAX_BODY,
        "maxCommandInputBytes": MAX_COMMAND_INPUT,
        "maxOutputBytes": MAX_OUTPUT,
        "maxTasks": MAX_TASKS,
        "maxActive": MAX_ACTIVE,
    }


def _agent_listing():
    listing = []
    for agent in AGENTS.values():
        entry = {
            "id": agent["id"],
            "name": agent.get("name") or agent["id"],
            "adapter": agent["type"],
            "timeoutMs": agent.get("timeoutMs") or TIMEOUT_MS,
            "capabilities": {
                **agent["capabilities"],
                "cancellation": "request_only" if agent["type"] == "http" else "process_signal",
            },
        }
        if agent.get("description") is not None:
            entry["description"] = agent["description"]
        if agent.get("cwd") is not None:
            entry["cwd"] = agent["cwd"]
        if agent.get("allowedRoots") is not None:
            entry["allowedRoots"] = agent["allowedRoots"]
        listing.append(entry)
    return listing


def _list_tasks(session_id=None, status=None, limit=100):
    with LOCK:
        tasks = list(TASKS.values())
    if session_id is not None:
        tasks = [task for task in tasks if task.get("sessionId") == session_id]
    if status is not None:
        tasks = [task for task in tasks if task["status"] == status]
    tasks.sort(key=lambda task: task.get("createdAt") or "", reverse=True)
    return [visible(task) for task in tasks[:limit]]


def _wait_for_terminal(task_id, wait_ms):
    deadline = time.monotonic() + wait_ms / 1000.0
    with TASK_CONDITION:
        while True:
            task = TASKS.get(task_id)
            if task is None or task["status"] not in ("queued", "running"):
                return task
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return task
            TASK_CONDITION.wait(remaining)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "agent-relay"

    def log_message(self, *args):  # silence per-request logging
        pass

    def _json(self, status, value, headers=None):
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        for name, header in (headers or {}).items():
            self.send_header(name, header)
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
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            origin = self.headers.get("origin")
            if origin is not None and origin not in ALLOWED_ORIGINS:
                # Browsers send Origin on cross-site requests; CLI and MCP clients do not.
                return self._json(403, {"error": "origin_not_allowed"})
            if path.startswith("/v1/") and not self._authorized():
                return self._json(401, {"error": "unauthorized",
                                        "message": "send Authorization: Bearer <token>"},
                                  headers={"www-authenticate": "Bearer"})
            if path == "/healthz":
                if method == "GET":
                    with LOCK:
                        active, queued = ACTIVE, sum(
                            1 for task in QUEUE if task["status"] == "queued")
                    return self._json(200, {"ok": True, "agents": len(AGENTS),
                                            "registry": REGISTRY_PATH,
                                            "tasks": len(TASKS), "active": active,
                                            "queued": queued, "limits": _limits()})
                return self._json(405, {"error": "method_not_allowed"})
            if path == "/v1/agents":
                if method == "GET":
                    return self._json(200, {"agents": _agent_listing()})
                return self._json(405, {"error": "method_not_allowed"})
            if path == "/v1/tasks":
                if method == "POST":
                    return self._submit()
                if method == "GET":
                    return self._list(query)
                return self._json(405, {"error": "method_not_allowed"})
            if path == "/v1/admin/reload":
                if method == "POST":
                    return self._reload()
                return self._json(405, {"error": "method_not_allowed"})
            match = re.fullmatch(r"/v1/tasks/([^/]+)", path)
            if match:
                task_id = match.group(1)
                if method == "GET":
                    return self._get_task(task_id, query)
                if method == "DELETE":
                    return self._cancel(TASKS.get(task_id))
                return self._json(405, {"error": "method_not_allowed"})
            return self._json(404, {"error": "not_found"})
        except Exception as exc:  # noqa: BLE001 - never leak a stack trace to the client
            self._json(500, {"error": "request_failed", "message": str(exc)})

    def _authorized(self):
        header = self.headers.get("authorization") or ""
        scheme, _, supplied = header.partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(
            supplied.strip().encode("utf-8"), TOKEN.encode("utf-8"))

    def _submit(self):
        content_type = (self.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self.close_connection = True
            return self._json(415, {"error": "unsupported_media_type",
                                    "message": "content-type must be application/json"})
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
        unknown = sorted(set(request) - TASK_FIELDS)
        if unknown:
            return self._json(400, {"error": "unknown_field",
                                    "message": f"Unknown request field: {unknown[0]}"})
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
        task_cwd, cwd_error = _resolve_task_cwd(agent, request.get("cwd"))
        if cwd_error:
            message = (f"cwd is not inside an allowed root for agent {agent['id']}"
                       if cwd_error == "cwd_not_allowed"
                       else f"cwd is not a directory: {request.get('cwd')}")
            return self._json(400, {"error": cwd_error, "message": message})
        if agent["type"] == "command" and len(input_text.encode("utf-8")) > MAX_COMMAND_INPUT:
            return self._json(413, {"error": "command_input_too_large"})
        if timeout_ms is not None:
            task_timeout = timeout_ms
        elif agent.get("timeoutMs"):
            task_timeout = agent["timeoutMs"]
        else:
            task_timeout = TIMEOUT_MS
        if MAX_TIMEOUT_MS and task_timeout > MAX_TIMEOUT_MS:
            return self._json(400, {
                "error": "invalid_timeout",
                "message": f"timeoutMs exceeds A2A_RELAY_MAX_TIMEOUT_MS ({MAX_TIMEOUT_MS})"})
        request_key = None if request_id is None else f"{agent['id']}\0{request_id}"
        with LOCK:
            prior = REQUEST_IDS.get(request_key) if request_key else None
            if prior is not None:
                if (prior["input"] != input_text
                        or prior.get("_requested_session_id") != session_id
                        or prior.get("_requested_timeout_ms") != timeout_ms
                        or prior.get("_requested_cwd") != request.get("cwd")):
                    return self._json(409, {"error": "idempotency_conflict"})
                return self._json(200, visible(prior))
            if not _make_room():
                return self._json(503, {"error": "task_capacity_reached"})
            task = {
                "id": str(uuid.uuid4()),
                "sessionId": session_id or str(uuid.uuid4()),
                "agentId": agent["id"],
                "input": input_text,
                "status": "queued",
                "createdAt": now_iso(),
                "timeoutMs": task_timeout,
            }
            if task_cwd:
                task["cwd"] = task_cwd
            if request_key:
                task["requestId"] = request_id
                task["_request_key"] = request_key
                task["_requested_session_id"] = session_id
                task["_requested_timeout_ms"] = timeout_ms
                task["_requested_cwd"] = request.get("cwd")
                REQUEST_IDS[request_key] = task
            TASKS[task["id"]] = task
            QUEUE.append(task)
        self._json(202, visible(task))
        _drain()

    def _get_task(self, task_id, query):
        wait_raw = query.get("waitMs", [None])[0]
        if wait_raw is None:
            task = TASKS.get(task_id)
        else:
            try:
                wait_ms = int(wait_raw)
            except ValueError:
                return self._json(400, {"error": "invalid_wait"})
            if wait_ms <= 0 or wait_ms > MAX_WAIT_MS:
                return self._json(400, {"error": "invalid_wait"})
            task = _wait_for_terminal(task_id, wait_ms)
        return self._json(200, visible(task)) if task else self._json(404, {"error": "unknown_task"})

    def _list(self, query):
        session_id = query.get("sessionId", [None])[0]
        status = query.get("status", [None])[0]
        if session_id is not None and not _valid(session_id):
            return self._json(400, {"error": "invalid_session_id"})
        if status is not None and status not in STATUSES:
            return self._json(400, {"error": "invalid_status"})
        limit = 100
        limit_raw = query.get("limit", [None])[0]
        if limit_raw is not None:
            try:
                limit = int(limit_raw)
            except ValueError:
                return self._json(400, {"error": "invalid_limit"})
            if limit <= 0:
                return self._json(400, {"error": "invalid_limit"})
        limit = min(limit, MAX_TASKS)
        return self._json(200, {"tasks": _list_tasks(session_id, status, limit)})

    def _reload(self):
        if self.client_address[0] not in ("127.0.0.1", "::1", "localhost"):
            return self._json(403, {"error": "forbidden"})
        try:
            path = reload_registry()
        except (OSError, ValueError) as exc:
            return self._json(400, {"error": "configuration_error", "message": str(exc)})
        return self._json(200, {"ok": True, "agents": len(AGENTS), "registry": str(path)})

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
                TASK_CONDITION.notify_all()
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


def _reload_signal(_signum, _frame):
    try:
        path = reload_registry()
    except (OSError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr, flush=True)
        return
    print(f"Reloaded registry from {path} ({len(AGENTS)} agents)", flush=True)


def main():
    global AGENTS, REGISTRY_PATH, TOKEN
    if not is_loopback(HOST) and not ALLOW_NON_LOOPBACK:
        print(f"error: refusing to listen on non-loopback address {HOST}: the relay has no TLS and "
              "is a single-user local service. Put it behind an authenticated HTTPS proxy and set "
              "AGENT_RELAY_UNSAFE_ALLOW_NON_LOOPBACK=1 to accept the risk.", file=sys.stderr)
        return 1
    path = config_path()
    try:
        AGENTS = load_registry(str(path))
        REGISTRY_PATH = str(path)
    except (OSError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _reload_signal)
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(f"error: port {PORT} is in use (another relay?); set A2A_RELAY_PORT",
                  file=sys.stderr)
            return 1
        print(f"error: {exc}", file=sys.stderr)
        return 1
    server.daemon_threads = True
    try:
        TOKEN = load_token()
    except (OSError, ValueError) as exc:
        server.server_close()
        print(f"error: cannot load the API token: {exc}", file=sys.stderr)
        return 1
    if not is_loopback(HOST):
        print(f"WARNING: listening on non-loopback address {HOST} without TLS; anyone who can "
              "reach it and obtain the token can run your coding agents.", file=sys.stderr, flush=True)
    print(f"agent-relay listening at http://{HOST}:{PORT} (registry: {path})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
