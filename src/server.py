#!/usr/bin/env python3
"""agent-relay: a local HTTP task service for delegating work between coding agents.

The service owns task IDs, queueing, status, timeouts, cancellation, idempotency, and a
registry of adapters. See README.md for the HTTP API and the relay.adapter/v1 contract.
"""
from __future__ import annotations

import errno
import hmac
import http.client
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

import pressure
from workflows import WORKFLOW_STATUSES, WorkflowEngine

ADAPTER_PROTOCOL = "relay.adapter/v1"
INHERITED_ENV = (
    "PATH", "HOME", "USER", "SHELL", "TMPDIR", "LANG", "LC_ALL",
    "SystemRoot", "ComSpec", "PATHEXT",
)
# The relay credential in the environment; a read-only sandboxed task never receives either name.
TOKEN_ENV = ("AGENT_RELAY_TOKEN", "A2A_RELAY_TOKEN")
AGENT_ID_RE = re.compile(r"[A-Za-z0-9_.~-]+")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
TASK_FIELDS = {"agentId", "input", "sessionId", "requestId", "timeoutMs", "cwd", "parentTaskId",
               "waitMs", "options", "readOnly"}
STATUSES = {"queued", "running", "completed", "failed", "timed_out", "cancelled"}
# On POSIX each adapter runs in its own process group so cancellation reaches descendants.
PROCESS_GROUPS = os.name == "posix"
# Seconds to wait for adapter pipes to drain after the adapter process has exited.
COLLECT_GRACE_S = 2.0
# macOS ships sandbox-exec; a readOnly profile with "sandbox": true needs it.
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Seconds to wait for each git call that locates the git metadata of a sandboxed task's cwd.
GIT_TIMEOUT_S = 5
# Where the relay looks for git when the owner names none (AGENT_RELAY_GIT). It never searches PATH.
# These directories belong to the system, so a task running as the relay's user cannot replace the file.
TRUSTED_GIT_PATHS = ("/usr/bin/git", "/bin/git")


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
# A workflow document declares at most this many steps, and the relay stores at most this many
# runs (the oldest finished one is evicted, as tasks are).
MAX_STEPS = _positive_int(64, "AGENT_RELAY_MAX_STEPS", "A2A_RELAY_MAX_STEPS")
MAX_WORKFLOWS = _positive_int(200, "AGENT_RELAY_MAX_WORKFLOWS", "A2A_RELAY_MAX_WORKFLOWS")
MAX_FOREACH = _positive_int(32, "AGENT_RELAY_MAX_FOREACH", "A2A_RELAY_MAX_FOREACH")
# Workflow journals are state, not configuration: they live beside relay.log, not beside the token.
STATE_DIR = Path(_env("AGENT_RELAY_STATE_DIR", "A2A_RELAY_STATE_DIR")
                 or Path.home() / ".local" / "state" / "agent-relay")
# Device pressure: stop active runs when the host reaches critical, checking this often. The
# guard decides nothing on an idle relay, so an unused relay never shells out to probe.
PRESSURE_GUARD = pressure.guard_enabled()
PRESSURE_INTERVAL_MS = _positive_int(5000, "AGENT_RELAY_PRESSURE_INTERVAL_MS",
                                     "A2A_RELAY_PRESSURE_INTERVAL_MS")
# Terminal tasks (their input, output, error, and cwd) are dropped this long after they finish;
# 0 keeps them until they are evicted for space or the relay restarts.
TASK_RETENTION_MS = _non_negative_int(0, "AGENT_RELAY_TASK_RETENTION_MS",
                                      "A2A_RELAY_TASK_RETENTION_MS")
# Recursive delegation: how many levels below a root task, and how many tasks one root's tree
# may create in total. 0 depth rejects every task that names a parentTaskId.
MAX_DELEGATION_DEPTH = _non_negative_int(2, "AGENT_RELAY_MAX_DELEGATION_DEPTH",
                                         "A2A_RELAY_MAX_DELEGATION_DEPTH")
MAX_DELEGATED_TASKS = _positive_int(20, "AGENT_RELAY_MAX_DELEGATED_TASKS",
                                    "A2A_RELAY_MAX_DELEGATED_TASKS")
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
_WARNED_FALLBACK = False
UNKNOWN_REVISION = {"commit": "unknown", "short": "unknown", "date": "unknown"}
REVISION = UNKNOWN_REVISION


def _trusted_git():
    """The absolute path of the git the relay runs, or None when it has none. Call once, at startup.

    The relay runs git outside any sandbox (to locate a repository's metadata, and for its own
    revision), as the user a read-only task runs as. A task may write anywhere outside the trees it
    protects, so a git found through PATH could be one the task planted. The relay therefore never
    searches PATH: the owner may name git in AGENT_RELAY_GIT (read here, from the relay's own
    environment, which a task cannot change), else the first of TRUSTED_GIT_PATHS that is an
    executable file. The owner's choice must be an absolute path to an executable file, or the
    relay exits; it should be a file the agents cannot write.
    """
    override = _env("AGENT_RELAY_GIT")
    if override is not None:
        if not (os.path.isabs(override) and os.path.isfile(override) and os.access(override, os.X_OK)):
            sys.exit("AGENT_RELAY_GIT must be an absolute path to an executable file")
        return override
    return next((path for path in TRUSTED_GIT_PATHS
                 if os.path.isfile(path) and os.access(path, os.X_OK)), None)


TRUSTED_GIT = _trusted_git()


def _git_env():
    """The environment git runs in: a fixed PATH, the relay's HOME and no configuration but the repository's.

    Built from nothing, so no variable of the relay's reaches git: not a GIT_* one that could point
    it at another repository or a hostile config, and not DEVELOPER_DIR or the like, which would
    redirect the /usr/bin/git shim on macOS. The system and the global configuration are switched
    off: a read-only task can write ~/.gitconfig, so git must not read it. Cost: a repository that
    needs safe.directory from the global configuration is refused, and the task fails closed.
    """
    env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    if os.environ.get("HOME"):
        env["HOME"] = os.environ["HOME"]
    return env


def deployed_revision(root):
    """Commit and commit date of the git checkout at ``root``.

    Every field is "unknown" when ``root`` is not the top of a git checkout or git cannot
    answer, so a missing git never stops the relay from starting. git is the trusted one, in the
    minimal environment (see _trusted_git and _git_env): a git planted on PATH must not run at startup.
    """
    if TRUSTED_GIT is None or not os.path.exists(os.path.join(root, ".git")):
        return dict(UNKNOWN_REVISION)
    try:
        result = subprocess.run([TRUSTED_GIT, "-C", root, "log", "-1", "--format=%H%n%h%n%cI"],
                                env=_git_env(), stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return dict(UNKNOWN_REVISION)
        commit, short, date = result.stdout.splitlines()
    except (OSError, subprocess.SubprocessError, ValueError):
        return dict(UNKNOWN_REVISION)
    return {"commit": commit, "short": short, "date": date}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def visible(task, include_input=False):
    """The task as clients see it. The prompt is left out unless asked for: callers already
    hold it, and echoing it on every poll fills their context. inputChars is always there."""
    snapshot = {key: value for key, value in task.items()
                if not key.startswith("_") and key != "input"}
    snapshot["inputChars"] = len(task["input"])
    if include_input:
        snapshot["input"] = task["input"]
    return snapshot


def default_config_path():
    """Prefer a per-user registry, then the one inside the checkout."""
    global _WARNED_FALLBACK
    user = Path.home() / ".config" / "agent-relay" / "agents.json"
    repo = Path(__file__).resolve().parent.parent / "config" / "agents.json"
    if user.exists():
        return user
    if not _WARNED_FALLBACK:
        print(f"warning: user registry not found at {user}; falling back to {repo}", file=sys.stderr)
        _WARNED_FALLBACK = True
    return repo


def config_path():
    if EXPLICIT_CONFIG:
        return Path(EXPLICIT_CONFIG)
    return default_config_path()


def token_path():
    explicit = _env("AGENT_RELAY_TOKEN_FILE", "A2A_RELAY_TOKEN_FILE")
    return Path(explicit) if explicit else Path.home() / ".config" / "agent-relay" / "token"


def load_token():
    """Return the API credential: AGENT_RELAY_TOKEN, else the token file (created 0600 if absent).

    Raises ValueError when the token file is a symlink, is empty, or (on POSIX) is accessible
    to other users. The mode is checked on the opened descriptor, not on the path.
    """
    value = (_env("AGENT_RELAY_TOKEN", "A2A_RELAY_TOKEN") or "").strip()
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
    if path.is_symlink():  # checked everywhere; O_NOFOLLOW also closes the race on POSIX
        raise ValueError(f"token file {path} is a symlink; replace it with a regular file")
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        if os.name == "posix" and os.fstat(handle.fileno()).st_mode & 0o077:
            raise ValueError(f"token file {path} is accessible to other users; run: chmod 600 {path}")
        value = handle.read().strip()
    if not value:
        raise ValueError(f"token file {path} is empty; delete it to generate a new token, "
                         "or set AGENT_RELAY_TOKEN")
    return value


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    # A dual-stack socket reports IPv4 clients as ::ffff:a.b.c.d.
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_loopback or bool(mapped and mapped.is_loopback)


def _check_options(agent_id, options):
    """Raise ValueError unless ``options`` maps option names to {value: [args]} objects."""
    def bad(problem):
        return ValueError(f"Invalid options: agent {agent_id}: {problem}")

    if not isinstance(options, dict):
        raise bad("options must be an object")
    for name, values in options.items():
        if not name:
            raise bad("option names must be non-empty strings")
        if not isinstance(values, dict):
            raise bad(f"option {name} must be an object")
        if not values:
            raise bad(f"option {name} must declare at least one value")
        for value, args in values.items():
            if not value:
                raise bad(f"option {name} has an empty value name")
            if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
                raise bad(f"option {name} value {value} must be an array of strings")


def _check_read_only(agent_id, agent_type, profile):
    """Raise ValueError unless ``profile`` is a readOnly profile that enforces something.

    A profile is {"args": [...], "sandbox": bool, "inputSeparator": str}. ``inputSeparator`` is the
    argument the relay puts before the task input. Only a command agent gets its input on the command
    line, so a stdio agent may not declare one.
    """
    def bad(problem):
        return ValueError(f"Invalid readOnly: agent {agent_id}: {problem}")

    if not isinstance(profile, dict):
        raise bad("readOnly must be an object")
    unknown = sorted(set(profile) - {"args", "sandbox", "inputSeparator"})
    if unknown:
        raise bad(f"readOnly has an unknown field {unknown[0]} (allowed: args, sandbox, inputSeparator)")
    if "inputSeparator" in profile:
        if agent_type != "command":
            raise bad("readOnly inputSeparator needs a command agent: a stdio agent gets its input on stdin")
        if not isinstance(profile["inputSeparator"], str) or not profile["inputSeparator"]:
            raise bad("readOnly inputSeparator must be a non-empty string")
    args = profile.get("args", [])
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise bad("readOnly args must be an array of strings")
    sandbox = profile.get("sandbox", False)
    if not isinstance(sandbox, bool):
        raise bad("readOnly sandbox must be true or false")
    if not args and not sandbox:
        raise bad("readOnly must set args, sandbox, or both; an empty profile would enforce nothing")


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
        if raw.get("options") is not None:
            if agent_type == "http":
                raise ValueError(f"Invalid options: agent {agent_id}: options need a command or stdio agent")
            _check_options(agent_id, raw["options"])
        if raw.get("readOnly") is not None:
            if agent_type == "http":
                raise ValueError(f"Invalid readOnly: agent {agent_id}: readOnly needs a command or stdio agent")
            _check_read_only(agent_id, agent_type, raw["readOnly"])
        delegate_to = raw.get("delegateTo")
        if delegate_to is not None and (not isinstance(delegate_to, list) or not all(
                isinstance(item, str) for item in delegate_to)):
            raise ValueError(f"Invalid delegateTo: {agent_id}")
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


def task_environment(agent, task):
    env = environment(agent)
    if task.get("_sandbox"):
        # A read-only agent must not hold the relay credential, even when the owner passes it
        # through (inheritEnv or env) for the agent's ordinary runs.
        for name in TOKEN_ENV:
            env.pop(name, None)
    # Lets the relay's MCP tools (or any client) attribute delegated work to this task.
    env["AGENT_RELAY_PARENT_TASK_ID"] = task["id"]
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

    The group is signalled while the leader is unreaped. After it has been reaped the group is
    signalled only where the leader's identity can still be checked (see _group_is_ours), so
    descendants it left behind are stopped on Linux but not on other POSIX systems.
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

    Returns None when the process did not start or run: the task was cancelled before or while it
    started (a process that raced with cancellation is terminated before it can run unobserved), or
    this call ended the task itself because it timed out before the start or could not be sandboxed.
    """
    with LOCK:
        if task["status"] != "running":
            return None
    if task.get("_sandbox"):
        try:
            argv = _sandboxed(agent, task, argv)
        except GitUnknown as exc:
            if _remaining(task) > 0:  # without time left the deadline check below ends the task
                _finish(task, "failed", error=(
                    f"read_only_git_unknown: cannot protect the git metadata of {_task_cwd(agent, task)}, "
                    f"so the task did not start: {exc}. Fix git for this directory (the relay ignores the "
                    "global git configuration, so safe.directory there does not apply), "
                    "or submit without readOnly"))
                return None
    if _remaining(task) <= 0:  # git discovery may have used the task's time up
        _finish(task, "timed_out", error=f"Agent exceeded {task['timeoutMs']} ms")
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


def _sandbox_available():
    return sys.platform == "darwin" and os.access(SANDBOX_EXEC, os.X_OK)


class GitUnknown(Exception):
    """git could not say where a repository's git metadata is, so the relay cannot protect it."""


def _looks_like_git_dir(path):
    """True when ``path`` has what git itself asks of a git directory: a HEAD, an objects/ and a refs/."""
    head = os.path.join(path, "HEAD")
    return ((os.path.isfile(head) or os.path.islink(head))
            and os.path.isdir(os.path.join(path, "objects")) and os.path.isdir(os.path.join(path, "refs")))


def _git_entry(directory):
    """The nearest sign of a repository at or above ``directory``, or None when there is none.

    A sign is a ``.git`` entry (file or directory), or a directory that is itself a git directory, as
    a bare repository is: git finds it from any directory inside it, and it has no ``.git``.
    """
    for candidate in (Path(directory), *Path(directory).parents):
        if os.path.lexists(candidate / ".git"):
            return str(candidate / ".git")
        if _looks_like_git_dir(candidate):
            return str(candidate)
    return None


def _git_path(cwd, arg, deadline=None):
    """What ``git rev-parse arg`` prints for ``cwd`` as an absolute path; raises GitUnknown on any failure.

    Failures are: git missing, slower than GIT_TIMEOUT_S or than the time left before ``deadline`` (a
    time.monotonic() value), a non-zero exit (not a repository, unsafe owner), and output that is not an
    existing directory (an old git echoes an option it does not know). It runs outside the sandbox, so
    it is the relay's trusted git by absolute path, in the minimal environment of _git_env: a git a
    task planted on PATH, or a ~/.gitconfig it wrote, must not run or apply. The path is read as bytes:
    it ends in git's one newline and may contain others. A relative path is relative to ``cwd``.
    """
    timeout = GIT_TIMEOUT_S if deadline is None else min(GIT_TIMEOUT_S, deadline - time.monotonic())
    if timeout <= 0:
        raise GitUnknown("the task's time ran out first")
    if TRUSTED_GIT is None:
        raise GitUnknown("the relay has no trusted git (it never searches PATH); "
                         "set AGENT_RELAY_GIT to an absolute path in the relay's environment")
    try:
        done = subprocess.run([TRUSTED_GIT, "-C", cwd, "rev-parse", arg], env=_git_env(),
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GitUnknown(f"git rev-parse {arg} took longer than {timeout:.1f} s") from None
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise GitUnknown(f"git rev-parse {arg} could not run: {exc}") from None
    if done.returncode != 0:
        said = done.stderr.decode("utf-8", "replace").strip()[:200] or "no message"
        raise GitUnknown(f"git rev-parse {arg} exited {done.returncode}: {said}")
    printed = done.stdout[:-1] if done.stdout.endswith(b"\n") else done.stdout
    path = os.path.join(cwd, os.fsdecode(printed))
    if not printed or not os.path.isdir(path):
        raise GitUnknown(f"git rev-parse {arg} printed {path!r}, which is not a directory")
    return path


def _git_metadata(cwd, deadline=None):
    """The real paths of the git directory and the shared git directory of the repository around ``cwd``.

    A linked worktree keeps its index, HEAD and reflog under the main repository's
    .git/worktrees/<name>, and its refs, objects and config in the main repository's .git. The same
    holds for a repository whose .git is a file and for a cwd below the top of a checkout. None of
    these is under the cwd, so a deny on the cwd alone would let a read-only agent rewrite them (for
    example ``git --work-tree=/tmp/x checkout <rev> -- <path>`` rewrites the real worktree's index).

    Fails closed. With no sign of a repository at or above the cwd (a ``.git`` entry, or a directory
    that looks like a git directory, as a bare repository does) the cwd is outside any repository, git
    is not asked, and the result is []. With one, git must answer: any failure raises GitUnknown,
    because a read-only task must not run with less protection than it promises. A directory that only
    looks like a git directory, and that git does not accept, is therefore refused too.
    """
    real = os.path.realpath(cwd)
    entry = _git_entry(real)
    if entry is None:
        return []
    try:
        paths = [_git_path(real, arg, deadline) for arg in ("--absolute-git-dir", "--git-common-dir")]
    except GitUnknown as exc:
        raise GitUnknown(f"{exc} (git metadata was expected at {entry})") from None
    return list(dict.fromkeys(os.path.realpath(path) for path in paths))


def _above(path):
    """Every directory above ``path``, up to but not including "/"."""
    return [str(parent) for parent in Path(path).parents][:-1]


def _sandboxed(agent, task, argv):
    """Wrap ``argv`` in a macOS sandbox that stops the process changing the tree it is reviewing.

    It cannot write under the task's cwd, the agent's allowedRoots, or the git metadata of the cwd
    (the git directory and the shared one, which for a worktree live outside the cwd). Reading them
    stays allowed, so git log, diff and show work. Everything else stays allowed, so the agent can
    still write its own state elsewhere and reach its model API. It cannot rename or delete a
    directory above any of these trees (or above the token file) either: moving /tmp/review away
    would let it write through the new path, which the deny on /tmp/review/tree no longer matches.
    Writes beside a protected tree, inside such a directory, stay allowed. The process runs as
    the relay's user, so two more rules stop it asking the relay to write for it, outside the sandbox:
    it cannot read the relay's token file (or move it, or a directory above it, to a path the read
    rule does not name), and it cannot connect to the relay's TCP port on any address. Paths and the
    port go in as -D parameters, never as profile text, so a value cannot change the profile. The
    profile matches real paths, so each path is resolved first. Raises GitUnknown when the cwd is in
    a repository whose git metadata cannot be found (see _git_metadata).
    """
    cwd = _task_cwd(agent, task)
    protected = list(dict.fromkeys(os.path.realpath(path) for path in
                                   [cwd, *(agent.get("allowedRoots") or []),
                                    *_git_metadata(cwd, task.get("_deadline"))]))
    token = os.path.realpath(token_path())
    above = list(dict.fromkeys(parent for path in [token, *protected] for parent in _above(path)))
    params = {"TOKEN": token, "RELAY_PORT": str(PORT)}
    params.update((f"P{index}", path) for index, path in enumerate(protected))
    params.update((f"A{index}", path) for index, path in enumerate(above))
    trees = " ".join(f'(subpath (param "P{index}"))' for index in range(len(protected)))
    profile = (
        "(version 1)(allow default)"
        f"(deny file-write* {trees})"
        '(deny file-read* file-write* (literal (param "TOKEN")))'
        '(deny network-outbound (remote tcp (string-append "*:" (param "RELAY_PORT"))))'
    )
    if above:  # a filter-less deny would block every unlink and rename
        parents = " ".join(f'(literal (param "A{index}"))' for index in range(len(above)))
        profile += f"(deny file-write-unlink {parents})"
    defines = [flag for name, value in params.items() for flag in ("-D", f"{name}={value}")]
    return [SANDBOX_EXEC, *defines, "-p", profile, *argv]


def _agent_argv(agent, task):
    """The command line without the task input: the agent's args, then the args its options map to."""
    return [agent["command"], *agent["args"], *task["_extra_args"]]


def _run_command(agent, task):
    env = task_environment(agent, task)
    env["A2A_TASK_ID"] = task["id"]
    env["A2A_SESSION_ID"] = task["sessionId"]
    try:
        proc = _spawn(agent, task, [*_agent_argv(agent, task), task["input"]], env,
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
    env = task_environment(agent, task)
    try:
        proc = _spawn(agent, task, _agent_argv(agent, task), env, subprocess.PIPE)
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


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    """Never follow adapter redirects: the prompt must only reach the configured URL."""

    def redirect_request(self, *args, **kwargs):
        return None


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

    opener = urlrequest.build_opener(_NoRedirect)
    payload = json.dumps(adapter_request(task)).encode("utf-8")
    request = urlrequest.Request(agent["url"], data=payload, method="POST",
                                 headers={"content-type": "application/json"})
    started = time.monotonic()
    try:
        with opener.open(request, timeout=max(_remaining(task), 0.001)) as response:
            content_type = (response.headers.get("content-type") or "").lower()
            raw = _read_body(response, task)
            code = response.status
            ok = 200 <= code < 300
    except urlerror.HTTPError as exc:
        if 300 <= exc.code < 400:
            location = exc.headers.get("location") if exc.headers else None
            _finish(task, "failed", error=f"HTTP adapter redirected ({exc.code} to {location}); "
                                          "redirects are not followed")
            exc.close()
            return
        content_type = ((exc.headers.get("content-type") if exc.headers else "") or "").lower()
        try:
            raw = _read_body(exc, task)
        except (OSError, socket.timeout) as read_error:
            _finish(task, "timed_out" if _remaining(task) <= 0 else "failed", error=str(read_error))
            return
        finally:
            exc.close()  # read1 never closes a fully read response; release the connection
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
    # One deadline covers process startup, request delivery, execution, and reading an HTTP
    # adapter's response; a spawned adapter's output is collected under a short post-exit grace.
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
            extra_args, sandbox, error = _dispatch_plan(agent, task)
            if error:
                _finish(task, "failed", error=f"{error[0]}: {error[1]}")
                continue
            task["_extra_args"], task["_sandbox"] = extra_args, sandbox
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



def _pressure_guard():
    """Stop active runs when the host reaches critical pressure. An idle relay never probes."""
    while not SHUTTING_DOWN:
        time.sleep(PRESSURE_INTERVAL_MS / 1000.0)
        try:
            if not WORKFLOW_ENGINE.active_ids():
                continue
            reading = pressure.snapshot()
            if reading["verdict"] != "critical":
                continue
            killed = WORKFLOW_ENGINE.kill_all(
                "device_pressure: " + "; ".join(reading["reasons"]))
            if killed:
                print(f"device pressure critical: stopped {len(killed)} workflow run(s); "
                      f"resume them when the machine recovers", flush=True)
        except Exception as exc:  # noqa: BLE001 - the guard must never take the relay down
            print(f"pressure guard error: {exc}", file=sys.stderr, flush=True)


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


def _resolve_options(agent, requested):
    """Return (options, args, error) for the options a task asks for.

    ``options`` is the request normalised (None when empty) and ``args`` the fixed arguments the
    registry maps it to, in the registry's order. Only registry text reaches the command line:
    a caller's value selects an entry and is never passed on. ``error`` is an (error, message)
    pair for a 400 response.
    """
    if requested is None or requested == {}:
        return None, [], None
    if not isinstance(requested, dict) or not all(
            isinstance(name, str) and name and isinstance(value, str)
            for name, value in requested.items()):
        return None, [], ("invalid_options",
                          "options must be an object of option names to value strings")
    declared = agent.get("options") or {}
    if not declared:
        return None, [], ("unknown_option", f"agent {agent['id']} declares no options")
    for name, value in requested.items():
        if name not in declared:
            return None, [], ("unknown_option", f"unknown option {name} for agent {agent['id']}; "
                                                f"allowed options: {', '.join(declared)}")
        if value not in declared[name]:
            return None, [], ("invalid_option_value",
                              f"option {name} does not allow {value!r}; "
                              f"allowed values: {', '.join(declared[name])}")
    args = [arg for name, values in declared.items() if name in requested
            for arg in values[requested[name]]]
    return dict(requested), args, None


def _read_only_refusal(agent):
    """Why this agent cannot take a read-only task on this host, or None when it can."""
    profile = agent.get("readOnly")
    if profile is None:
        return (f"agent {agent['id']} has no readOnly profile, so it cannot promise a read-only run "
                "(GET /v1/agents shows which agents support readOnly)")
    if profile.get("sandbox") and not _sandbox_available():
        return (f"agent {agent['id']} runs read-only tasks under sandbox-exec, "
                "which is available on macOS only")
    return None


def _cwd_required(agent, task_cwd):
    """Why a sandboxed read-only task has no tree to protect, or None. ``agent`` has a readOnly profile."""
    if agent["readOnly"].get("sandbox") and not (task_cwd or agent.get("cwd")):
        return (f"agent {agent['id']} runs read-only tasks in a sandbox that protects the task's cwd; "
                "pass cwd, or set cwd on the agent")
    return None


def _input_refusal(agent, task_input):
    """Why a read-only task's input may not go on the command line as it is, or None.

    ``agent`` has a readOnly profile. A command agent gets the input as its last argument, after the
    profile's args and with nothing between them. An input that starts with "-" is then an option, and
    with most parsers the last one wins: input "--access=write" would undo the profile's
    "--access=read". So the profile must say how the input is separated: with an ``inputSeparator``
    (for example "--") the relay puts it before the input and any input is data; without one, an
    input that starts with "-" is refused. A stdio agent gets its input on stdin, so nothing is
    refused. A profile without args has no argument for the input to override: the sandbox is its
    contract.
    """
    profile = agent["readOnly"]
    if (agent["type"] != "command" or not profile.get("args") or profile.get("inputSeparator")
            or not task_input.startswith("-")):
        return None
    return (f"agent {agent['id']} takes its input as a command-line argument, and an input that starts with "
            "\"-\" could be read as an option that overrides its read-only arguments: start the input with "
            "another character, or ask the owner to set inputSeparator (for example \"--\") in the agent's "
            "readOnly profile")


def _dispatch_plan(agent, task):
    """Re-check a queued task against the registry entry that is about to run it.

    Returns (extra_args, sandbox, error). The registry may have been reloaded since the task was
    accepted, so what was valid then proves nothing now: the task's options must still resolve, and a
    read-only task needs an entry that still promises it on this host and still lets its input onto the
    command line. ``error`` is the (code, message) that submission would give, and the task must not
    run. The extra args and the sandbox flag come from the entry as it is now, so a task never runs
    with the options or profile of an entry that is gone.
    """
    _, option_args, error = _resolve_options(agent, task.get("options"))
    if error:
        return None, False, error
    if not task.get("readOnly"):
        return option_args, False, None
    refusal = _read_only_refusal(agent)
    if refusal:
        return None, False, ("read_only_unsupported", refusal)
    refusal = _cwd_required(agent, task.get("cwd"))
    if refusal:
        return None, False, ("cwd_required", refusal)
    refusal = _input_refusal(agent, task["input"])
    if refusal:
        return None, False, ("invalid_input", refusal)
    profile = agent["readOnly"]
    # The profile's args go last, so no option's args can follow and override them, then the separator
    # that keeps the input (appended after these) from being read as an option.
    separator = [profile["inputSeparator"]] if profile.get("inputSeparator") else []
    return option_args + profile.get("args", []) + separator, bool(profile.get("sandbox")), None


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
        "maxSteps": MAX_STEPS,
        "maxWorkflows": MAX_WORKFLOWS,
        "maxForEach": MAX_FOREACH,
        "maxDelegationDepth": MAX_DELEGATION_DEPTH,
        "maxDelegatedTasks": MAX_DELEGATED_TASKS,
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
        if agent.get("delegateTo") is not None:
            entry["delegateTo"] = agent["delegateTo"]
        if agent.get("options"):
            entry["options"] = {name: list(values) for name, values in agent["options"].items()}
        if _read_only_refusal(agent) is None:
            entry["readOnly"] = "supported"
            entry["readOnlySandboxed"] = bool(agent["readOnly"].get("sandbox"))
        else:
            entry["readOnly"] = "unsupported"
        listing.append(entry)
    return listing


def _delegation_error(parent, agent):
    """Return (status, error, message) when the parent task may not delegate to ``agent``."""
    parent_agent = AGENTS.get(parent["agentId"])
    if parent_agent is None:
        # Fail closed: without the parent's registry entry its delegateTo cannot be checked.
        return 403, "delegation_not_allowed", (
            f"agent {parent['agentId']} is no longer registered, so it may not delegate")
    allowed = parent_agent.get("delegateTo")
    if allowed is not None and agent["id"] not in allowed:
        return 403, "delegation_not_allowed", (
            f"agent {parent['agentId']} may not delegate to {agent['id']} (see its delegateTo in GET /v1/agents)")
    if agent["id"] in parent["_lineage"]:
        return 409, "delegation_cycle", (
            f"{agent['id']} is already in this delegation chain: {' > '.join(parent['_lineage'])}")
    if len(parent["_lineage"]) > MAX_DELEGATION_DEPTH:
        return 409, "delegation_depth_exceeded", (
            f"delegation depth would exceed AGENT_RELAY_MAX_DELEGATION_DEPTH ({MAX_DELEGATION_DEPTH})")
    if parent["_root"]["_delegated"] >= MAX_DELEGATED_TASKS:
        return 429, "delegation_budget_exhausted", (
            f"this task tree already created AGENT_RELAY_MAX_DELEGATED_TASKS ({MAX_DELEGATED_TASKS}) tasks")
    return None


def _list_tasks(session_id=None, status=None, limit=100):
    with LOCK:
        tasks = list(TASKS.values())
    if session_id is not None:
        tasks = [task for task in tasks if task.get("sessionId") == session_id]
    if status is not None:
        tasks = [task for task in tasks if task["status"] == status]
    tasks.sort(key=lambda task: task.get("createdAt") or "", reverse=True)
    return [visible(task) for task in tasks[:limit]]


def _is_valid_wait(value):
    return _is_positive_int(value) and value <= MAX_WAIT_MS


def _invalid_wait():
    return {"error": "invalid_wait", "message": f"waitMs must be an integer from 1 to {MAX_WAIT_MS}"}


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


def _create_task(request):
    """Validate one task request and enqueue it. Returns ``(status, body)``.

    Called by ``POST /v1/tasks``, so every task request is validated in one place.
    """
    if not isinstance(request, dict):
        return 400, {"error": "invalid_request"}
    unknown = sorted(set(request) - TASK_FIELDS)
    if unknown:
        return 400, {"error": "unknown_field",
                     "message": f"Unknown request field: {unknown[0]}"}
    agent = AGENTS.get(request.get("agentId"))
    if agent is None:
        return 404, {"error": "unknown_agent"}
    input_text = request.get("input")
    if not _valid(input_text, MAX_BODY):
        return 400, {"error": "input_required"}
    session_id = request.get("sessionId")
    if session_id is not None and not _valid(session_id):
        return 400, {"error": "invalid_session_id"}
    request_id = request.get("requestId")
    if request_id is not None and not _valid(request_id):
        return 400, {"error": "invalid_request_id"}
    timeout_ms = request.get("timeoutMs")
    if timeout_ms is not None and not _is_positive_int(timeout_ms):
        return 400, {"error": "invalid_timeout"}
    parent_id = request.get("parentTaskId")
    if parent_id is not None and not _valid(parent_id):
        return 400, {"error": "invalid_parent_task_id"}
    # waitMs only names the wait the caller means to use next. Refuse a bad one here, before the
    # task exists: a caller that gets this error then holds no task ID for a task that runs on.
    if "waitMs" in request and not _is_valid_wait(request["waitMs"]):
        return 400, _invalid_wait()
    options, _, option_error = _resolve_options(agent, request.get("options"))
    if option_error:
        return 400, {"error": option_error[0], "message": option_error[1]}
    # Present but not a boolean (even null) is refused: a safety flag must not fall back to writable.
    read_only = request.get("readOnly", False)
    if not isinstance(read_only, bool):
        return 400, {"error": "invalid_read_only",
                     "message": "readOnly must be true or false"}
    refusal = _read_only_refusal(agent) if read_only else None
    if refusal:
        return 400, {"error": "read_only_unsupported", "message": refusal}
    task_cwd, cwd_error = _resolve_task_cwd(agent, request.get("cwd"))
    if cwd_error:
        message = (f"cwd is not inside an allowed root for agent {agent['id']}"
                   if cwd_error == "cwd_not_allowed"
                   else f"cwd is not a directory: {request.get('cwd')}")
        return 400, {"error": cwd_error, "message": message}
    # Without a cwd the sandbox would have no tree to protect, so the run would not be read-only.
    no_cwd = _cwd_required(agent, task_cwd) if read_only else None
    if no_cwd:
        return 400, {"error": "cwd_required", "message": no_cwd}
    bad_input = _input_refusal(agent, input_text) if read_only else None
    if bad_input:
        return 400, {"error": "invalid_input", "message": bad_input}
    if agent["type"] == "command" and len(input_text.encode("utf-8")) > MAX_COMMAND_INPUT:
        return 413, {"error": "command_input_too_large"}
    if timeout_ms is not None:
        task_timeout = timeout_ms
    elif agent.get("timeoutMs"):
        task_timeout = agent["timeoutMs"]
    else:
        task_timeout = TIMEOUT_MS
    if MAX_TIMEOUT_MS and task_timeout > MAX_TIMEOUT_MS:
        return 400, {
            "error": "invalid_timeout",
            "message": f"timeoutMs exceeds A2A_RELAY_MAX_TIMEOUT_MS ({MAX_TIMEOUT_MS})"}
    request_key = None if request_id is None else f"{agent['id']}\0{request_id}"
    with LOCK:
        prior = REQUEST_IDS.get(request_key) if request_key else None
        if prior is not None:
            if (prior["input"] != input_text
                    or prior.get("_requested_session_id") != session_id
                    or prior.get("_requested_timeout_ms") != timeout_ms
                    or prior.get("_requested_cwd") != request.get("cwd")
                    or prior.get("options") != options
                    or bool(prior.get("readOnly")) != read_only
                    or prior.get("parentTaskId") != parent_id):
                return 409, {"error": "idempotency_conflict"}
            return 200, visible(prior)
        parent = TASKS.get(parent_id) if parent_id is not None else None
        if parent_id is not None:
            if parent is None:
                return 400, {"error": "unknown_parent_task"}
            refused = _delegation_error(parent, agent)
            if refused:
                status, error, message = refused
                return status, {"error": error, "message": message}
        if not _make_room():
            return 503, {"error": "task_capacity_reached"}
        task = {
            "id": str(uuid.uuid4()),
            "sessionId": session_id or str(uuid.uuid4()),
            "agentId": agent["id"],
            "input": input_text,
            "status": "queued",
            "createdAt": now_iso(),
            "timeoutMs": task_timeout,
        }
        if parent is None:
            task.update(depth=0, _lineage=[agent["id"]], _delegated=0)
            task["_root"] = task
        else:
            task.update(parentTaskId=parent["id"], depth=parent["depth"] + 1,
                        _lineage=parent["_lineage"] + [agent["id"]], _root=parent["_root"])
            parent["_root"]["_delegated"] += 1
        if task_cwd:
            task["cwd"] = task_cwd
        if options:
            task["options"] = options
        if read_only:
            task["readOnly"] = True
        if request_key:
            task["requestId"] = request_id
            task["_request_key"] = request_key
            task["_requested_session_id"] = session_id
            task["_requested_timeout_ms"] = timeout_ms
            task["_requested_cwd"] = request.get("cwd")
            REQUEST_IDS[request_key] = task
        TASKS[task["id"]] = task
        QUEUE.append(task)
    return 202, visible(task)


def cancel_task(task):
    """Cancel a queued or running task in place and return it. ``None`` stays ``None``.

    A running task keeps its active slot until its worker returns, so the relay never runs
    more than MAX_ACTIVE adapters at once.
    """
    if task is None:
        return None
    with LOCK:
        if task["status"] in ("queued", "running"):
            if task["status"] == "running":
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
    return task



def lookup_task(task_id):
    with LOCK:
        return TASKS.get(task_id)


WORKFLOW_ENGINE = WorkflowEngine(
    lock=LOCK,
    condition=TASK_CONDITION,
    create_task=_create_task,
    get_task=lookup_task,
    cancel_task=cancel_task,
    drain=_drain,
    agents=lambda: AGENTS,
    resolve_cwd=_resolve_task_cwd,
    max_steps=MAX_STEPS,
    max_workflows=MAX_WORKFLOWS,
    max_foreach=MAX_FOREACH,
    state_dir=STATE_DIR,
    max_body=MAX_BODY,
    max_command_input=MAX_COMMAND_INPUT,
    max_timeout_ms=MAX_TIMEOUT_MS,
    now=now_iso,
)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "agent-relay"

    def log_message(self, *args):  # silence per-request logging
        pass

    def _json(self, status, value, headers=None):
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        if status >= 400:
            # An error may leave the request body unread; never parse it as a next request.
            self.close_connection = True
            self.send_header("connection", "close")
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
                # The body is left unread, so drop the connection rather than parse it as a request.
                self.close_connection = True
                return self._json(403, {"error": "origin_not_allowed"})
            if path.startswith("/v1/") and not self._authorized():
                self.close_connection = True
                return self._json(401, {"error": "unauthorized",
                                        "message": "send Authorization: Bearer <token>"},
                                  headers={"www-authenticate": "Bearer"})
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
                                            "queued": queued, "limits": _limits(),
                                            "revision": REVISION})
                return self._json(405, {"error": "method_not_allowed"})
            if path == "/v1/agents":
                if method == "GET":
                    return self._json(200, {"agents": _agent_listing(), "revision": REVISION})
                return self._json(405, {"error": "method_not_allowed"})
            if path == "/v1/tasks":
                if method == "POST":
                    return self._submit()
                if method == "GET":
                    return self._list(query)
                return self._json(405, {"error": "method_not_allowed"})
            if path == "/v1/pressure":
                if method == "GET":
                    return self._json(200, pressure.snapshot())
                return self._json(405, {"error": "method_not_allowed"})
            if path == "/v1/pressure/kill":
                if method == "POST":
                    return self._kill_workflows()
                return self._json(405, {"error": "method_not_allowed"})
            if path == "/v1/workflows":
                if method == "POST":
                    return self._submit_workflow()
                if method == "GET":
                    return self._list_workflows(query)
                return self._json(405, {"error": "method_not_allowed"})
            if path == "/v1/admin/reload":
                if method == "POST":
                    return self._reload()
                return self._json(405, {"error": "method_not_allowed"})
            match = re.fullmatch(r"/v1/workflows/([^/]+)/resume", path)
            if match:
                if method == "POST":
                    return self._resume_workflow(match.group(1))
                return self._json(405, {"error": "method_not_allowed"})
            match = re.fullmatch(r"/v1/workflows/([^/]+)", path)
            if match:
                workflow_id = match.group(1)
                if method == "GET":
                    return self._get_workflow(workflow_id, query)
                if method == "DELETE":
                    return self._delete_workflow(workflow_id)
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
        if not TOKEN:
            return False  # never authorize against an unset token
        header = self.headers.get("authorization") or ""
        scheme, _, supplied = header.partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(
            supplied.strip().encode("utf-8"), TOKEN.encode("utf-8"))

    def _submit(self):
        error, request = self._read_json_body()
        if error:
            return self._json(error, request)
        status, body = _create_task(request)
        self._json(status, body)
        if status == 202:
            _drain()

    def _read_json_body(self, document=True):
        """Read the request body as a JSON object.

        Returns ``(error_status, error_body)`` on failure and ``(None, body)`` on success, so
        every caller sends exactly one response. Pass ``document=False`` for a route that
        takes no document: the body is still read, so a keep-alive connection stays in sync.
        """
        if document:
            content_type = (self.headers.get("content-type") or "").split(";")[0].strip().lower()
            if content_type != "application/json":
                self.close_connection = True
                return 415, {"error": "unsupported_media_type",
                             "message": "content-type must be application/json"}
        length_header = self.headers.get("content-length")
        length = int(length_header) if length_header and length_header.isdigit() else 0
        if length > MAX_BODY:
            self.close_connection = True
            return 413, {"error": "request_failed", "message": "body too large"}
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return 400, {"error": "invalid_json"}
        if not isinstance(body, dict):
            return 400, {"error": "invalid_request"}
        return None, body

    def _get_task(self, task_id, query):
        include_input = query.get("includeInput", ["false"])[0]
        if include_input not in ("true", "false"):
            return self._json(400, {"error": "invalid_include_input",
                                    "message": "includeInput must be true or false"})
        wait_raw = query.get("waitMs", [None])[0]
        wait_ms = None
        if wait_raw is None:
            task = TASKS.get(task_id)
        else:
            try:
                wait_ms = int(wait_raw)
            except ValueError:
                wait_ms = 0
            if not _is_valid_wait(wait_ms):
                return self._json(400, _invalid_wait())
            task = _wait_for_terminal(task_id, wait_ms)
        if task is None:
            return self._json(404, {"error": "unknown_task"})
        result = visible(task, include_input == "true")
        if wait_ms is not None and result["status"] in ("queued", "running"):
            # Say that the wait ended, so a caller need not compare status strings. Judge the
            # copy, not the live task: the task may finish between the wait and this check.
            result.update(waitExpired=True, waitedMs=wait_ms,
                          hint="The wait expired before the task finished. Call wait_task again "
                               "with the same taskId, or repeat this request.")
        return self._json(200, result)

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

    def _submit_workflow(self):
        error, spec = self._read_json_body()
        if error:
            return self._json(error, spec)
        # The engine asks this only when the submission would create a new run, so replaying a
        # requestId still returns the run that exists rather than a refusal that loses its id.
        status, body = WORKFLOW_ENGINE.submit(
            spec, refusal=lambda: pressure.should_refuse(pressure.snapshot()))
        self._json(status, body)

    def _kill_workflows(self):
        """The kill button: stop every active run now, keeping each journal for a resume."""
        # Consume any body so a keep-alive connection stays in sync; the button takes no input.
        self._read_json_body(document=False)
        reading = pressure.snapshot()
        detail = f" ({'; '.join(reading['reasons'])})" if reading["reasons"] else ""
        # Say that a human asked, not that the guard fired: the verdict here may be only ok.
        reason = (f"device_pressure: stopped on request while the verdict was "
                  f"{reading['verdict']}{detail}")
        killed = WORKFLOW_ENGINE.kill_all(reason)
        self._json(200, {"killed": killed, "reason": reason, "pressure": reading})

    def _list_workflows(self, query):
        session_id = query.get("sessionId", [None])[0]
        status = query.get("status", [None])[0]
        if session_id is not None and not _valid(session_id):
            return self._json(400, {"error": "invalid_session_id"})
        if status is not None and status not in WORKFLOW_STATUSES:
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
        return self._json(200, {"workflows": WORKFLOW_ENGINE.list(session_id, status, limit)})

    def _get_workflow(self, workflow_id, query):
        wait_raw = query.get("waitMs", [None])[0]
        if wait_raw is None:
            return self._workflow_response(workflow_id)
        try:
            wait_ms = int(wait_raw)
        except ValueError:
            return self._json(400, _invalid_wait())
        if not _is_valid_wait(wait_ms):
            return self._json(400, _invalid_wait())
        if WORKFLOW_ENGINE.wait(workflow_id, wait_ms) is None:
            return self._workflow_not_found(workflow_id)
        view = WORKFLOW_ENGINE.public(workflow_id, detail=True)
        if view is None:
            return self._workflow_not_found(workflow_id)
        if view["status"] in ("queued", "running"):
            # Say that the wait ended, so a caller need not compare status strings.
            view.update(waitExpired=True, waitedMs=wait_ms,
                        hint="The wait expired before the run finished. Call get_workflow again "
                             "with the same workflowId, or repeat this request.")
        return self._json(200, view)

    def _workflow_response(self, workflow_id):
        view = WORKFLOW_ENGINE.public(workflow_id, detail=True)
        if view is None:
            return self._workflow_not_found(workflow_id)
        return self._json(200, view)

    def _workflow_not_found(self, workflow_id):
        if WORKFLOW_ENGINE.journaled(workflow_id):
            return self._json(404, {
                "error": "unknown_workflow",
                "message": f"The run is not active; POST /v1/workflows/{workflow_id}/resume"
                           f" to continue it from its journal"})
        return self._json(404, {"error": "unknown_workflow"})

    def _delete_workflow(self, workflow_id):
        view = WORKFLOW_ENGINE.cancel(workflow_id)
        if view is None:
            return self._workflow_not_found(workflow_id)
        self._json(200, view)
        _drain()

    def _resume_workflow(self, workflow_id):
        # Resume takes no document, but the body is still read so a keep-alive connection
        # stays in sync for the next request on it.
        self._read_json_body(document=False)
        status, body = WORKFLOW_ENGINE.resume(workflow_id)
        self._json(status, body)

    def _reload(self):
        if not is_loopback(self.client_address[0]):
            return self._json(403, {"error": "forbidden"})
        try:
            path = reload_registry()
        except (OSError, ValueError) as exc:
            return self._json(400, {"error": "configuration_error", "message": str(exc)})
        return self._json(200, {"ok": True, "agents": len(AGENTS), "registry": str(path)})

    def _cancel(self, task):
        if task is None:
            return self._json(404, {"error": "unknown_task"})
        cancel_task(task)
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
    global AGENTS, REGISTRY_PATH, REVISION, TOKEN
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
    REVISION = deployed_revision(str(Path(__file__).resolve().parent.parent))
    if TASK_RETENTION_MS:
        threading.Thread(target=_purge_periodically, daemon=True).start()
    if PRESSURE_GUARD:
        threading.Thread(target=_pressure_guard, daemon=True).start()
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
