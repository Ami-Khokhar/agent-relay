#!/usr/bin/env python3
"""agent-relay: a local HTTP task service for delegating work between coding agents.

The service owns task IDs, queueing, status, timeouts, cancellation, idempotency, and a
registry of adapters. See README.md for the HTTP API and the relay.adapter/v1 contract.
"""
from __future__ import annotations

import errno
import http.client
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
# On POSIX each adapter runs in its own process group so cancellation reaches descendants.
PROCESS_GROUPS = os.name == "posix"
# Seconds to wait for adapter pipes to drain after the adapter process has exited.
COLLECT_GRACE_S = 2.0


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
# Terminal tasks (their input, output, error, and cwd) are dropped this long after they finish;
# 0 keeps them until they are evicted for space or the relay restarts.
TASK_RETENTION_MS = _non_negative_int(0, "AGENT_RELAY_TASK_RETENTION_MS",
                                      "A2A_RELAY_TASK_RETENTION_MS")
EXPLICIT_CONFIG = _env("AGENT_RELAY_AGENTS_FILE", "A2A_AGENTS_FILE")

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
    # read1 returns what is available; read(n) would wait for n bytes or EOF.
    read = getattr(stream, "read1", stream.read)

    def run():
        try:
            while True:
                chunk = read(65536)
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


def _start_time(pid):
    """The process start time from /proc (Linux), or None when it cannot be read."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().rsplit(b")", 1)[1].split()
    except (OSError, IndexError):
        return None
    return fields[19] if len(fields) > 19 else None  # field 22 of stat; fields[0] is field 3


def _group_is_ours(proc):
    """Whether the process group named by the adapter's pid still belongs to this task.

    Until the leader is reaped its pid, and so the group id, cannot be reused. Afterwards the
    id stays reserved only while group members remain; once it is free another process may
    take it. On Linux the leader's start time, recorded at spawn, tells the two apart: a process
    now holding that pid with a different start time is foreign. Elsewhere there is no way to
    check, so a reaped leader's group is never signalled.
    """
    if proc.returncode is None:
        return True
    recorded = getattr(proc, "relay_start_time", None)
    if recorded is None:
        return False
    current = _start_time(proc.pid)
    # No process with that pid: the id is either free (the signal fails with ESRCH) or still
    # held by our surviving group members.
    return current is None or current == recorded


def _signal(proc, force):
    try:
        if PROCESS_GROUPS:
            if not _group_is_ours(proc):
                return
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
        elif proc.poll() is None:
            proc.kill() if force else proc.terminate()
    except OSError:
        pass


def _terminate(proc):
    """SIGTERM the adapter's process group (or the process itself), then SIGKILL after 1 s.

    The group is signalled even when the direct child has already exited, so descendants it
    left behind are stopped too.
    """
    if not PROCESS_GROUPS and proc.poll() is not None:
        return
    _signal(proc, force=False)
    killer = threading.Timer(1.0, _signal, (proc, True))
    killer.daemon = True
    killer.start()


def _remaining(task):
    return max(0.0, task["_deadline"] - time.monotonic())


def _wait(proc, task):
    try:
        proc.wait(timeout=_remaining(task))
        return False
    except subprocess.TimeoutExpired:
        _terminate(proc)
        proc.wait()
        return True


def _join_all(threads, seconds):
    deadline = time.monotonic() + seconds
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return not any(thread.is_alive() for thread in threads)


def _collect(proc, threads):
    """Wait a bounded time for the adapter's I/O threads once the adapter has exited.

    A descendant that still holds a pipe (for example one that called setsid(), or any child on
    Windows) could block a write or delay EOF forever. After a short grace the group is signalled
    once more and then the relay stops waiting: the daemon I/O threads are abandoned, whatever
    output was captured is used, and the task's slot is released.
    """
    if not _join_all(threads, COLLECT_GRACE_S):
        _terminate(proc)
        _join_all(threads, 1.5)


def _spawn(agent, task, argv, env, stdin):
    """Start an adapter process and register it with the task in one step.

    Returns None when the task was cancelled before or while the process started; a process
    that raced with cancellation is terminated before it can run unobserved.
    """
    with LOCK:
        if task["status"] != "running":
            return None
    proc = subprocess.Popen(
        argv, cwd=_task_cwd(agent, task), env=env, stdin=stdin,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=PROCESS_GROUPS,
    )
    proc.relay_start_time = _start_time(proc.pid)
    with LOCK:
        task["_proc"] = proc
        cancelled = task["status"] != "running"
    if cancelled:
        _terminate(proc)
    return proc


def _feed(proc, data):
    """Write the request to stdin on a thread so a non-reading adapter cannot block the deadline."""
    result = {}

    def run():
        try:
            proc.stdin.write(data)
            proc.stdin.close()
        except (OSError, ValueError) as exc:
            result["error"] = getattr(exc, "strerror", None) or str(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


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
        proc = _spawn(agent, task, [agent["command"], *agent["args"], task["input"]], env,
                      subprocess.DEVNULL)
    except OSError as exc:
        _finish(task, "failed", error=exc.strerror or str(exc))
        return
    if proc is None:
        return
    capture = _Capture(MAX_OUTPUT)
    out, err = [], []
    readers = (_pump(proc.stdout, out, capture), _pump(proc.stderr, err, capture))
    timed_out = _wait(proc, task)
    _collect(proc, readers)
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
        proc = _spawn(agent, task, [agent["command"], *agent["args"]], env, subprocess.PIPE)
    except OSError as exc:
        _finish(task, "failed", error=exc.strerror or str(exc))
        return
    if proc is None:
        return
    capture = _Capture(MAX_OUTPUT)
    out, err = [], []
    readers = (_pump(proc.stdout, out, capture), _pump(proc.stderr, err, capture))
    writer, fed = _feed(proc, (json.dumps(adapter_request(task)) + "\n").encode("utf-8"))
    timed_out = _wait(proc, task)
    _collect(proc, (writer, *readers))
    stdin_error = fed.get("error")
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


def _read_body(stream, task):
    """Read at most MAX_OUTPUT + 1 bytes, giving up at the task deadline.

    The socket timeout alone bounds each recv, so an adapter that drip-feeds bytes could
    otherwise hold the task (and its active slot) far past its timeout. The loop ends when
    read1 returns nothing (Content-Length consumed, chunked terminator, or EOF) or the
    response reports that it is closed.
    """
    # ``stream`` is an HTTPResponse, or an HTTPError wrapping one in ``fp``; the socket
    # timeout must be set on the response that owns the connection.
    response = stream if isinstance(stream, http.client.HTTPResponse) else getattr(stream, "fp", None)
    if not isinstance(response, http.client.HTTPResponse):
        response = None
    read = response.read1 if response is not None else stream.read
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    closed = response.isclosed if response is not None else (lambda: False)
    chunks, size = [], 0
    while size <= MAX_OUTPUT and not closed():
        remaining = _remaining(task)
        if remaining <= 0:
            raise TimeoutError(f"Agent exceeded {task['timeoutMs']} ms")
        if sock is not None:
            sock.settimeout(remaining)
        chunk = read(min(65536, MAX_OUTPUT + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _run_http(agent, task):
    from urllib import error as urlerror
    from urllib import request as urlrequest

    payload = json.dumps(adapter_request(task)).encode("utf-8")
    request = urlrequest.Request(agent["url"], data=payload, method="POST",
                                 headers={"content-type": "application/json"})
    started = time.monotonic()
    try:
        with urlrequest.urlopen(request, timeout=max(_remaining(task), 0.001)) as response:
            content_type = (response.headers.get("content-type") or "").lower()
            raw = _read_body(response, task)
            code = response.status
            ok = 200 <= code < 300
    except urlerror.HTTPError as exc:
        content_type = ((exc.headers.get("content-type") if exc.headers else "") or "").lower()
        try:
            raw = _read_body(exc, task)
        except (OSError, socket.timeout) as read_error:
            _finish(task, "timed_out" if _remaining(task) <= 0 else "failed", error=str(read_error))
            return
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
    # One deadline covers process startup, request delivery, execution, and result collection.
    task["_deadline"] = time.monotonic() + task["timeoutMs"] / 1000.0
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
    finally:
        _release(task)


def _finish(task, status, **fields):
    with LOCK:
        if task["status"] not in ("queued", "running"):
            return
        task["status"] = status
        task["finishedAt"] = now_iso()
        task["_finished"] = time.monotonic()
        task.update({key: value for key, value in fields.items() if value is not None})
        TASK_CONDITION.notify_all()


def _release(task):
    """Free the task's active slot once its worker, and so its adapter process, is done.

    A cancelled task keeps its slot until the process exits (or the HTTP request returns), so
    the relay never runs more than MAX_ACTIVE adapters at once.
    """
    global ACTIVE
    with LOCK:
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
                task["_finished"] = time.monotonic()
                task["error"] = "unknown_agent"
                TASK_CONDITION.notify_all()
                continue
            ACTIVE += 1
            task["_counted"] = True
            task["_adapter"] = agent["type"]
            task["status"] = "running"
            task["startedAt"] = now_iso()
            threading.Thread(target=_worker, args=(agent, task), daemon=True).start()


def _forget(task):
    del TASKS[task["id"]]
    request_key = task.get("_request_key")
    if request_key:
        REQUEST_IDS.pop(request_key, None)
    TASK_CONDITION.notify_all()


def _purge_expired():
    """Drop terminal tasks that finished more than TASK_RETENTION_MS ago. Call with LOCK held."""
    if not TASK_RETENTION_MS:
        return
    cutoff = time.monotonic() - TASK_RETENTION_MS / 1000.0
    for task in [task for task in TASKS.values() if task.get("_finished", cutoff + 1) <= cutoff]:
        _forget(task)


def _purge_periodically():
    """Enforce retention on an idle relay too, not only when a request arrives."""
    interval = min(TASK_RETENTION_MS / 1000.0, 60.0)
    while True:
        time.sleep(interval)
        with LOCK:
            _purge_expired()


def _make_room():
    if len(TASKS) < MAX_TASKS:
        return True
    terminal = next((task for task in TASKS.values()
                     if task["status"] not in ("queued", "running")), None)
    if terminal is None:
        return False
    _forget(terminal)
    return True


def _terminate_task(task):
    proc = task.get("_proc")
    if proc is not None:
        _terminate(proc)


def _cancellation_mode(agent_type):
    return "request_only" if agent_type == "http" else "process_signal"


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
        "taskRetentionMs": TASK_RETENTION_MS or None,
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
                "cancellation": _cancellation_mode(agent["type"]),
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
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path.startswith("/v1/"):
                with LOCK:
                    _purge_expired()
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
        if task is None:
            return self._json(404, {"error": "unknown_task"})
        with LOCK:
            if task["status"] in ("queued", "running"):
                if task["status"] == "running":
                    # The worker keeps the active slot until the adapter has really stopped.
                    _terminate_task(task)
                    task["cancellation"] = _cancellation_mode(task.get("_adapter"))
                else:
                    try:
                        QUEUE.remove(task)
                    except ValueError:
                        pass
                task["status"] = "cancelled"
                task["finishedAt"] = now_iso()
                task["_finished"] = time.monotonic()
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
    global AGENTS, REGISTRY_PATH
    path = config_path()
    try:
        AGENTS = load_registry(str(path))
        REGISTRY_PATH = str(path)
    except (OSError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    if TASK_RETENTION_MS:
        threading.Thread(target=_purge_periodically, daemon=True).start()
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
