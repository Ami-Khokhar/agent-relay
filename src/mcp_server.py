#!/usr/bin/env python3
"""agent-relay MCP stdio interface.

A thin JSON-RPC 2.0 proxy over the relay HTTP API. Exposes the tools list_agents,
delegate, wait_task, get_task, list_tasks, list_sessions, and cancel_task. Set A2A_RELAY_URL to point at a
non-default relay. When the relay is not reachable and the URL is loopback, the interface
starts the HTTP service detached on first use.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

STATUSES = ("queued", "running", "completed", "failed", "timed_out", "cancelled")
FALSE_VALUES = ("0", "false", "no", "off")


def _env(*names, default=None):
    for name in names:
        value = os.environ.get(name)
        if value is not None and value != "":
            return value
    return default


RELAY_URL = (_env("AGENT_RELAY_URL", "A2A_RELAY_URL") or "http://127.0.0.1:43124").rstrip("/")
_raw_timeout = _env("AGENT_RELAY_HTTP_TIMEOUT_MS", "A2A_RELAY_HTTP_TIMEOUT_MS")
if _raw_timeout is not None:
    try:
        REQUEST_TIMEOUT_MS = int(_raw_timeout)
    except ValueError:
        sys.exit("A2A_RELAY_HTTP_TIMEOUT_MS must be a positive integer")
    if REQUEST_TIMEOUT_MS <= 0:
        sys.exit("A2A_RELAY_HTTP_TIMEOUT_MS must be a positive integer")
else:
    REQUEST_TIMEOUT_MS = 10_000

TOOLS = [
    {
        "name": "list_agents",
        "description": "List the agents registered with the relay, with their adapter, capabilities, and timeout. Call this first to get valid agentId values.",
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "delegate",
        "description": "Send a new task to another coding agent (pi, Codex, OpenCode, Claude Code). Use for delegation, orchestration, fan-out, and second opinions. Returns the task object (the existing task when requestId matches an earlier submission); pass its `id` field to wait_task, get_task, or cancel_task.",
        "inputSchema": {
            "type": "object", "required": ["agentId", "input"], "additionalProperties": False,
            "properties": {
                "agentId": {"type": "string", "minLength": 1, "maxLength": 128,
                            "description": "ID of the target agent. Get valid IDs from list_agents. Do not invent an ID."},
                "input": {"type": "string", "minLength": 1,
                          "description": "Full task text for the target agent. The agent cannot see this conversation. Include the goal, relevant file paths, constraints, and the expected output format."},
                "sessionId": {"type": "string", "minLength": 1, "maxLength": 128,
                              "description": "Correlation tag for grouping related tasks; not a native harness session, and each task is a fresh invocation. Reuse the value from an earlier task, or a sessionId from list_sessions, to group work and to filter list_tasks. Omit it and the relay assigns a new UUID (returned in the task result)."},
                "sessionName": {"type": "string", "minLength": 1, "maxLength": 128,
                                "description": "Human-readable name for the session. Used only when this call creates a new session; rename an existing session with PATCH /v1/sessions/:id."},
                "requestId": {"type": "string", "minLength": 1, "maxLength": 128,
                              "description": "Idempotency key. When you retry a delegate call after an error or timeout, send the same requestId so the relay returns the existing task instead of starting a second one. Use a new value for each new task."},
                "timeoutMs": {"type": "integer", "minimum": 1,
                              "description": "Maximum run time for the task, in milliseconds. When it expires the relay stops waiting, marks the task timed_out, and terminates a command/stdio adapter; an http adapter's work may continue. Omit to use the agent's default timeout."},
                "cwd": {"type": "string", "minLength": 1,
                        "description": "Working directory for the target agent (absolute path recommended). Must be inside the agent's allowedRoots or equal to its default cwd, else the relay rejects it with cwd_not_allowed; it must also be an existing directory, otherwise the relay rejects it with invalid_cwd. Omit to use the agent's default cwd."},
                "waitMs": {"type": "integer", "minimum": 1,
                           "description": "Maximum time, in milliseconds, that this call waits for the task to finish; the relay rejects values above A2A_RELAY_MAX_WAIT_MS (default 600000) with invalid_wait (the task is still created). If the task is still running, the call returns its current status; then call wait_task again with the same taskId."},
            },
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
    },
    {
        "name": "wait_task",
        "description": "Long-poll until a relay task is terminal or maxWaitMs elapses",
        "inputSchema": {
            "type": "object", "required": ["taskId"], "additionalProperties": False,
            "properties": {
                "taskId": {"type": "string", "minLength": 1, "maxLength": 128,
                           "description": "Task ID returned by delegate."},
                "maxWaitMs": {"type": "integer", "minimum": 1,
                              "description": "Maximum time to wait, in milliseconds, before returning the current status; the relay rejects values above A2A_RELAY_MAX_WAIT_MS (default 600000) with invalid_wait."},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "get_task",
        "description": "Get the current state and result of a relay task",
        "inputSchema": {
            "type": "object", "required": ["taskId"], "additionalProperties": False,
            "properties": {"taskId": {"type": "string", "minLength": 1, "maxLength": 128,
                                      "description": "Task ID returned by delegate."}},
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "list_tasks",
        "description": "List stored relay tasks, optionally filtered by session or status",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "sessionId": {"type": "string", "minLength": 1, "maxLength": 128,
                              "description": "Filter to one session. Get the ID from a delegate result. Letters, digits, and _ . ~ - only."},
                "status": {"type": "string", "enum": list(STATUSES),
                           "description": "Filter by task status."},
                "limit": {"type": "integer", "minimum": 1,
                          "description": "Maximum number of tasks to return."},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "list_sessions",
        "description": "List recent relay sessions, newest activity first. Use it to find earlier sessions to reference or report on.",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "agentId": {"type": "string", "minLength": 1, "maxLength": 128,
                            "description": "Filter to sessions created by this agent."},
                "cwd": {"type": "string", "minLength": 1,
                        "description": "Filter to sessions whose working directory is this path."},
                "limit": {"type": "integer", "minimum": 1,
                          "description": "Maximum number of sessions to return (default 20)."},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "cancel_task",
        "description": "Cancel a queued or running relay task",
        "inputSchema": {
            "type": "object", "required": ["taskId"], "additionalProperties": False,
            "properties": {"taskId": {"type": "string", "minLength": 1, "maxLength": 128,
                                      "description": "Task ID returned by delegate."}},
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    },
]


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class RelayError(Exception):
    def __init__(self, status, data, message):
        super().__init__(message)
        self.status = status
        self.data = data
        self.message = message


def _invalid(message):
    raise RpcError(-32602, message)


def _is_positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _arguments_for(rpc, allowed):
    params = rpc.get("params") or {}
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        _invalid("arguments must be an object")
    for key in args:
        if key not in allowed:
            _invalid(f"Unknown argument: {key}")
    return args


def _non_empty(args, key, max_length=None):
    value = args.get(key)
    if not isinstance(value, str) or not value or (max_length and len(value) > max_length):
        suffix = f" of at most {max_length} characters" if max_length else ""
        _invalid(f"{key} must be a non-empty string{suffix}")


def _http_request(base_url, path, method="GET", body=None, timeout=10.0):
    from urllib import error as urlerror
    from urllib import request as urlrequest

    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    request = urlrequest.Request(base_url + path, data=data, method=method, headers=headers)
    try:
        with urlrequest.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            code = response.status
            ok = 200 <= code < 300
    except urlerror.HTTPError as exc:
        raw = exc.read()
        code = exc.code
        ok = False
    except (urlerror.URLError, socket.timeout, TimeoutError, OSError) as exc:
        raise RelayError(None, {"error": "relay_request_failed", "message": str(getattr(exc, "reason", exc))},
                         str(getattr(exc, "reason", exc)))
    try:
        value = json.loads(raw.decode("utf-8") or "null")
    except ValueError:
        value = {"error": "invalid_relay_response"}
    valid_object = isinstance(value, dict)
    if not ok:
        data_object = value if valid_object else {"error": "invalid_relay_response"}
        message = data_object.get("message") or data_object.get("error") or f"Relay returned {code}"
        raise RelayError(code, data_object, message)
    if not valid_object:
        raise RelayError(None, {}, "Relay returned a non-object response")
    return value


def _loopback(base_url):
    hostname = urlsplit(base_url).hostname
    return hostname in ("127.0.0.1", "localhost", "::1")


def _autostart_enabled():
    return (_env("AGENT_RELAY_AUTOSTART", "A2A_RELAY_AUTOSTART", default="1") or "").lower() \
        not in FALSE_VALUES


_autostart_lock = threading.Lock()


def _health_ok(base_url):
    try:
        _http_request(base_url, "/healthz", timeout=0.5)
        return True
    except RelayError:
        return False


def _maybe_start_relay(base_url):
    """Start src/server.py detached for a loopback URL. Returns True when it is reachable."""
    if not _autostart_enabled() or not _loopback(base_url):
        return False
    with _autostart_lock:
        if _health_ok(base_url):
            return True
        parsed = urlsplit(base_url)
        port = parsed.port or 43124
        server_path = Path(__file__).resolve().parent / "server.py"
        if not server_path.exists():
            return False
        env = dict(os.environ)
        env.setdefault("AGENT_RELAY_PORT", str(port))
        env.setdefault("A2A_RELAY_PORT", str(port))
        if parsed.hostname:
            env.setdefault("AGENT_RELAY_HOST", parsed.hostname)
        log_dir = Path.home() / ".local" / "state" / "agent-relay"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log = open(log_dir / "relay.log", "ab")
        except OSError:
            return False
        try:
            subprocess.Popen(
                [sys.executable, str(server_path)], cwd=str(server_path.parent.parent), env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError:
            return False
        finally:
            log.close()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if _health_ok(base_url):
                return True
            time.sleep(0.1)
        return False


def _relay_request(base_url, path, method="GET", body=None, timeout=10.0):
    """Issue a relay request, starting the HTTP service on demand for loopback URLs."""
    try:
        return _http_request(base_url, path, method=method, body=body, timeout=timeout)
    except RelayError as exc:
        if exc.status is not None:
            raise
        if not _maybe_start_relay(base_url):
            raise
        return _http_request(base_url, path, method=method, body=body, timeout=timeout)


def create_http_handler(base_url=None, timeout=None):
    base_url = base_url if base_url is not None else RELAY_URL
    timeout = timeout if timeout is not None else REQUEST_TIMEOUT_MS / 1000.0

    def handle(rpc):
        method = rpc.get("method")
        if method == "initialize":
            return {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "agent-relay", "version": "0.2.0"},
                "instructions": (
                    "agent-relay sends tasks to other coding agents (pi, Codex, OpenCode, "
                    "Claude Code) and returns their results. When the user asks to delegate, "
                    "orchestrate, fan out, or use another agent or model: call list_agents, "
                    "then delegate, then wait_task. Do not run agent CLIs (pi, codex, "
                    "opencode, claude) in a shell to do this work. The relay owns task IDs, "
                    "status, timeouts, cancellation, and idempotency. For parallel work, call "
                    "delegate once per task, then wait for each taskId. Reuse the sessionId "
                    "from an earlier task or list_sessions to group related work for "
                    "list_tasks; it does not resume the harness session. Omit it to start a "
                    "new session. If no listed agent fits, tell the user and ask before you "
                    "run a CLI directly."
                ),
            }
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "ping":
            return {}
        if method != "tools/call":
            raise RpcError(-32601, "Method not found")
        name = (rpc.get("params") or {}).get("name")
        try:
            if name == "list_agents":
                _arguments_for(rpc, [])
                value = _relay_request(base_url, "/v1/agents", timeout=timeout)
            elif name == "delegate":
                args = _arguments_for(rpc, ["agentId", "input", "sessionId", "sessionName",
                                            "requestId", "timeoutMs", "cwd", "waitMs"])
                _non_empty(args, "agentId", 128)
                _non_empty(args, "input")
                if "sessionId" in args:
                    _non_empty(args, "sessionId", 128)
                if "sessionName" in args:
                    _non_empty(args, "sessionName", 128)
                if "requestId" in args:
                    _non_empty(args, "requestId", 128)
                if "timeoutMs" in args and not _is_positive_int(args["timeoutMs"]):
                    _invalid("timeoutMs must be a positive integer")
                if "cwd" in args:
                    _non_empty(args, "cwd")
                if "waitMs" in args and not _is_positive_int(args["waitMs"]):
                    _invalid("waitMs must be a positive integer")
                payload = {"agentId": args["agentId"], "input": args["input"]}
                for key in ("sessionId", "sessionName", "requestId", "timeoutMs", "cwd"):
                    if key in args:
                        payload[key] = args[key]
                value = _relay_request(base_url, "/v1/tasks", method="POST", body=payload, timeout=timeout)
                wait_ms = args.get("waitMs")
                if wait_ms and value.get("status") in ("queued", "running"):
                    value = _relay_request(
                        base_url, f"/v1/tasks/{quote(value['id'], safe='')}?waitMs={wait_ms}",
                        timeout=max(timeout, wait_ms / 1000.0 + 5))
            elif name == "wait_task":
                args = _arguments_for(rpc, ["taskId", "maxWaitMs"])
                _non_empty(args, "taskId", 128)
                max_wait = args.get("maxWaitMs", 60_000)
                if not _is_positive_int(max_wait):
                    _invalid("maxWaitMs must be a positive integer")
                value = _relay_request(
                    base_url, f"/v1/tasks/{quote(args['taskId'], safe='')}?waitMs={max_wait}",
                    timeout=max(timeout, max_wait / 1000.0 + 5))
            elif name == "get_task":
                args = _arguments_for(rpc, ["taskId"])
                _non_empty(args, "taskId", 128)
                value = _relay_request(base_url, "/v1/tasks/" + quote(args["taskId"], safe=""), timeout=timeout)
            elif name == "list_tasks":
                args = _arguments_for(rpc, ["sessionId", "status", "limit"])
                params = {}
                if "sessionId" in args:
                    _non_empty(args, "sessionId", 128)
                    params["sessionId"] = args["sessionId"]
                if "status" in args:
                    if args["status"] not in STATUSES:
                        _invalid(f"status must be one of {', '.join(STATUSES)}")
                    params["status"] = args["status"]
                if "limit" in args:
                    if not _is_positive_int(args["limit"]):
                        _invalid("limit must be a positive integer")
                    params["limit"] = args["limit"]
                path = "/v1/tasks" + (f"?{urlencode(params)}" if params else "")
                value = _relay_request(base_url, path, timeout=timeout)
            elif name == "list_sessions":
                args = _arguments_for(rpc, ["agentId", "cwd", "limit"])
                params = {}
                if "agentId" in args:
                    _non_empty(args, "agentId", 128)
                    params["agentId"] = args["agentId"]
                if "cwd" in args:
                    _non_empty(args, "cwd")
                    params["cwd"] = args["cwd"]
                if "limit" in args:
                    if not _is_positive_int(args["limit"]):
                        _invalid("limit must be a positive integer")
                    params["limit"] = args["limit"]
                path = "/v1/sessions" + (f"?{urlencode(params)}" if params else "")
                value = _relay_request(base_url, path, timeout=timeout)
            elif name == "cancel_task":
                args = _arguments_for(rpc, ["taskId"])
                _non_empty(args, "taskId", 128)
                value = _relay_request(base_url, "/v1/tasks/" + quote(args["taskId"], safe=""),
                                       method="DELETE", timeout=timeout)
            else:
                raise RpcError(-32602, f"Unknown tool: {name or ''}")
        except RpcError:
            raise
        except RelayError as exc:
            data = exc.data or {}
            payload = {"error": data.get("error") or "relay_request_failed", "message": exc.message}
            if exc.status:
                payload["status"] = exc.status
            return {"content": [{"type": "text", "text": json.dumps(payload)}],
                    "structuredContent": payload, "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": value}

    return handle


def _write(output_stream, lock, value):
    with lock:
        output_stream.write(json.dumps(value) + "\n")
        output_stream.flush()


def serve(input_stream=None, output_stream=None, handler=None):
    input_stream = input_stream if input_stream is not None else sys.stdin
    output_stream = output_stream if output_stream is not None else sys.stdout
    handler = handler if handler is not None else create_http_handler()
    write_lock = threading.Lock()
    threads = []
    for line in input_stream:
        line = line.strip()
        if not line:
            continue
        try:
            rpc = json.loads(line)
        except ValueError:
            _write(output_stream, write_lock,
                   {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        if not isinstance(rpc, dict) or rpc.get("jsonrpc") != "2.0" or not isinstance(rpc.get("method"), str):
            request_id = rpc.get("id") if isinstance(rpc, dict) else None
            _write(output_stream, write_lock,
                   {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32600, "message": "Invalid Request"}})
            continue
        if "id" not in rpc:
            continue

        def run(rpc=rpc):
            try:
                result = handler(rpc)
            except RpcError as exc:
                response = {"jsonrpc": "2.0", "id": rpc["id"], "error": {"code": exc.code, "message": exc.message}}
            except Exception as exc:  # noqa: BLE001 - report as an internal JSON-RPC error
                response = {"jsonrpc": "2.0", "id": rpc["id"], "error": {"code": -32603, "message": str(exc)}}
            else:
                response = {"jsonrpc": "2.0", "id": rpc["id"], "result": result}
            _write(output_stream, write_lock, response)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    try:
        serve()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"{exc}\n")
        sys.exit(1)
