#!/usr/bin/env python3
"""agent-relay MCP stdio interface.

A thin JSON-RPC 2.0 proxy over the relay HTTP API. Exposes the six task tools and the six
workflow tools; tools/list is the authoritative list. Set A2A_RELAY_URL to point at a
non-default relay. When the relay is not reachable and the URL is loopback, the interface
starts the HTTP service detached on first use.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib import request as urlrequest
from urllib.parse import quote, urlencode, urlsplit

STATUSES = ("queued", "running", "completed", "failed", "timed_out", "cancelled")
WORKFLOW_STATUSES = ("queued", "running", "completed", "failed", "cancelled")
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


def _token():
    """The relay API token: AGENT_RELAY_TOKEN, else the token file the relay creates."""
    value = (_env("AGENT_RELAY_TOKEN", "A2A_RELAY_TOKEN") or "").strip()
    if value:
        return value
    path = _env("AGENT_RELAY_TOKEN_FILE", "A2A_RELAY_TOKEN_FILE")
    path = Path(path) if path else Path.home() / ".config" / "agent-relay" / "token"
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


# The relay validates workflow documents itself. This schema exists so an agent authoring a
# run can see the accepted shape from tools/list, without reading the HTTP reference.
WORKFLOW_SPEC = {
    "type": "object",
    "additionalProperties": False,
    "required": ["steps"],
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 128,
                 "description": "Label for the run. Defaults to \"workflow\"."},
        "sessionId": {"type": "string", "minLength": 1, "maxLength": 128,
                      "description": "Correlation tag passed to every task the run starts. "
                                     "Defaults to the run ID."},
        "requestId": {"type": "string", "minLength": 1, "maxLength": 128,
                      "description": "Idempotency key for the submission. Resend the same one "
                                     "to get the existing run instead of starting a second."},
        "steps": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "agentId", "input"],
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 128,
                           "pattern": "^[A-Za-z0-9_.~-]{1,128}$",
                           "description": "Unique in the document. Other steps name it in needs "
                                          "and read its result as {{id.output}}."},
                    "agentId": {"type": "string", "minLength": 1, "maxLength": 128,
                                "description": "ID of the target agent. Get valid IDs from "
                                               "list_agents."},
                    "input": {"type": "string", "minLength": 1,
                              "description": "Full prompt for that agent. May contain "
                                             "{{stepId.output}} for a step this one needs, and "
                                             "{{item}} / {{index}} on a foreach step."},
                    "needs": {"type": "array", "items": {"type": "string"},
                              "description": "Step ids that must complete first. A step with no "
                                             "needs starts immediately; the graph must be acyclic."},
                    "foreach": {"type": "array", "minItems": 1,
                                "items": {"type": "string", "minLength": 1, "maxLength": 1024},
                                "description": "Run this step once per item, in parallel, with "
                                               "{{item}} and {{index}} set. The step then yields a "
                                               "JSON array of the per-item outputs."},
                    "retries": {"type": "integer", "minimum": 0, "maximum": 5,
                                "description": "Extra attempts after the task fails or times out."},
                    "repeat": {
                        "type": "object", "additionalProperties": False,
                        "required": ["max", "untilOutputMatches"],
                        "description": "Re-run until the output matches. Use for a review or fix "
                                       "loop; not allowed with foreach.",
                        "properties": {
                            "max": {"type": "integer", "minimum": 1, "maximum": 10,
                                    "description": "Attempts before the step fails with "
                                                   "repeat_limit_reached."},
                            "untilOutputMatches": {"type": "string", "minLength": 1,
                                                   "description": "Python regular expression "
                                                                  "tested against the output."},
                        },
                    },
                    "timeoutMs": {"type": "integer", "minimum": 1,
                                  "description": "Per-step timeout. Defaults to the agent's."},
                    "cwd": {"type": "string", "minLength": 1,
                            "description": "Per-step working directory, inside the agent's "
                                           "allowedRoots."},
                    "options": {"type": "object", "additionalProperties": {"type": "string"},
                                "description": "Named options for that agent, exactly as for a "
                                               "task submission, for example {\"effort\": "
                                               "\"high\"}. list_agents shows the names and "
                                               "values each agent allows; the step fails with "
                                               "unknown_option if you invent one."},
                    "readOnly": {"type": "boolean",
                                 "description": "Run that step read-only, exactly as for a task "
                                                "submission. An agent whose list_agents entry "
                                                "does not show readOnly: supported fails the "
                                                "step with read_only_unsupported."},
                },
            },
        },
    },
}


TOOLS = [
    {
        "name": "list_agents",
        "description": "List the agents registered with the relay, with their adapter, capabilities, timeout, the per-task options each one allows (option names and allowed values), and whether it supports readOnly tasks. Call this first to get valid agentId values. The result also has `revision`: the commit (`commit`, `short`) and commit date (`date`) the relay runs, each \"unknown\" when the relay cannot read its checkout. A `date` weeks old means the install is stale; `revision` is absent on a relay that predates this field.",
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "delegate",
        "description": "Send a new task to another coding agent (pi, Codex, OpenCode, Claude Code). Use for delegation, orchestration, fan-out, and second opinions. When called from an agent the relay itself started, the relay enforces its delegation policy (allowed targets, no cycles, depth and task budgets) and may refuse the task. Returns the task object (the existing task when requestId matches an earlier submission); pass its `id` field to wait_task, get_task, or cancel_task. Task objects leave out `input` and carry `inputChars` (its length) instead; get_task with includeInput returns it.",
        "inputSchema": {
            "type": "object", "required": ["agentId", "input"], "additionalProperties": False,
            "properties": {
                "agentId": {"type": "string", "minLength": 1, "maxLength": 128,
                            "description": "ID of the target agent. Get valid IDs from list_agents. Do not invent an ID."},
                "input": {"type": "string", "minLength": 1,
                          "description": "Full task text for the target agent. The agent cannot see this conversation. Include the goal, relevant file paths, constraints, and the expected output format."},
                "sessionId": {"type": "string", "minLength": 1, "maxLength": 128,
                              "description": "Correlation tag for grouping related tasks; not a native harness session, and each task is a fresh invocation. Reuse the value from an earlier task to group work and to filter list_tasks. Omit it and the relay assigns a new UUID (returned in the task result)."},
                "requestId": {"type": "string", "minLength": 1, "maxLength": 128,
                              "description": "Idempotency key. When you retry a delegate call after an error or timeout, send the same requestId so the relay returns the existing task instead of starting a second one. Use a new value for each new task."},
                "timeoutMs": {"type": "integer", "minimum": 1,
                              "description": "Maximum run time for the task, in milliseconds. When it expires the relay stops waiting, marks the task timed_out, and terminates a command/stdio adapter; an http adapter's work may continue. Omit to use the agent's default timeout."},
                "cwd": {"type": "string", "minLength": 1,
                        "description": "Working directory for the target agent (absolute path recommended). Must be inside the agent's allowedRoots or equal to its default cwd, else the relay rejects it with cwd_not_allowed; it must also be an existing directory, otherwise the relay rejects it with invalid_cwd. Omit to use the agent's default cwd."},
                "options": {"type": "object", "additionalProperties": {"type": "string"},
                            "description": "Named options for the target agent, for example {\"effort\": \"high\"}. Each agent allows only the option names and values that list_agents shows for it; the relay refuses any other name or value (unknown_option, invalid_option_value) and adds only the arguments the owner mapped to the chosen values. Omit to use the agent's defaults."},
                "readOnly": {"type": "boolean",
                             "description": "Set true to require that the agent cannot change files: the relay adds the agent's read-only arguments and, where the owner enabled it on macOS, runs it in a sandbox that blocks writes under the task's cwd, the agent's allowedRoots and the cwd's git metadata, reading the relay token, and calling the relay (so that agent cannot delegate). It does not block reads or other network access. An agent that does not list readOnly: supported in list_agents refuses the task (read_only_unsupported) and nothing runs. If the cwd is in a git repository whose metadata git cannot locate, the task fails (read_only_git_unknown) instead of running with less protection. An input that starts with \"-\" is refused (invalid_input) when the agent takes its input as a command-line argument and the owner declared no input separator for it, because it could be read as an option that overrides the read-only arguments; start the input with another character. Omit or false for a normal run."},
                "waitMs": {"type": "integer", "minimum": 1,
                           "description": "Maximum time, in milliseconds, that this call waits for the task to finish. A value above the relay's limit (A2A_RELAY_MAX_WAIT_MS, default 600000) is clamped to that limit, never raised: the result then carries waitMs (the wait used) and waitClamped: true. The relay checks the wait before it creates the task, so a refused wait leaves no task and a retry with the same requestId cannot duplicate it. If the task is still running when the wait ends, the call returns its current state with waitExpired: true; then call wait_task again with the same taskId. Use about 110000 or less where a tool call longer than 120 s moves to the background."},
            },
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
    },
    {
        "name": "wait_task",
        "description": "Long-poll until a relay task is terminal or maxWaitMs elapses. If the task is still not terminal when the wait ends, the result carries waitExpired: true, waitedMs (the wait that elapsed), and a hint: call wait_task again with the same taskId. A terminal result has no waitExpired. A client with a short tool-call budget should use maxWaitMs of about 110000 or less and loop while waitExpired is true: Claude Code moves an MCP call that runs longer than 120 s to the background, and its completion notice then shows a task that is still running. The task object has `inputChars` and no `input`; use get_task with includeInput to read the input.",
        "inputSchema": {
            "type": "object", "required": ["taskId"], "additionalProperties": False,
            "properties": {
                "taskId": {"type": "string", "minLength": 1, "maxLength": 128,
                           "description": "Task ID returned by delegate."},
                "maxWaitMs": {"type": "integer", "minimum": 1,
                              "description": "Maximum time to wait, in milliseconds (default 60000), before returning the current status. A value above the relay's limit (A2A_RELAY_MAX_WAIT_MS, default 600000) is clamped to that limit: the result then carries waitMs (the wait used) and waitClamped: true. Use about 110000 or less where a tool call longer than 120 s moves to the background."},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "get_task",
        "description": "Get the current state and result of a relay task. The task object has `inputChars` (the length of the input) and leaves out `input` unless includeInput is true.",
        "inputSchema": {
            "type": "object", "required": ["taskId"], "additionalProperties": False,
            "properties": {
                "taskId": {"type": "string", "minLength": 1, "maxLength": 128,
                           "description": "Task ID returned by delegate."},
                "includeInput": {"type": "boolean",
                                 "description": "Set true to include the task's `input` in the result. Default false: you already hold the prompt you sent, so the relay does not echo it."},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "list_tasks",
        "description": "List stored relay tasks, optionally filtered by session or status. Task objects have `inputChars` and no `input`; use get_task with includeInput to read one.",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "sessionId": {"type": "string", "minLength": 1, "maxLength": 128,
                              "description": "Filter to one session. Get the ID from a delegate result."},
                "status": {"type": "string", "enum": list(STATUSES),
                           "description": "Filter by task status."},
                "limit": {"type": "integer", "minimum": 1,
                          "description": "Maximum number of tasks to return."},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "cancel_task",
        "description": "Cancel a queued or running relay task. For command/stdio agents the relay signals the adapter's process group (SIGTERM, then SIGKILL after 1 s). For http agents it only marks the task cancelled (cancellation: request_only); the in-flight request is not aborted and the remote service may keep working.",
        "inputSchema": {
            "type": "object", "required": ["taskId"], "additionalProperties": False,
            "properties": {"taskId": {"type": "string", "minLength": 1, "maxLength": 128,
                                      "description": "Task ID returned by delegate."}},
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    },
    {
        "name": "run_workflow",
        "description": "Submit a whole multi-agent workflow in one call: named steps, each bound to a registered agent, wired together with needs. Use this instead of calling delegate repeatedly when you already know the shape of the work: a pipeline (A writes, B reviews, C fixes), a fan-out over several items, or a review loop. The relay starts a step only once every step it needs has completed, threads each output into the next prompt, and owns the queue, timeouts, cancellation, and retries. A step whose dependency did not succeed is skipped and the run fails, so a broken review never reaches the fix step. The relay refuses the run with device_pressure while this machine is under critical host pressure, so call get_pressure first when the run will be long. Returns the run with every step's status; pass waitMs to block until it finishes. Get valid agentId values from list_agents first. The spec schema in tools/list describes every step field.",
        "inputSchema": {
            "type": "object", "required": ["spec"], "additionalProperties": False,
            "properties": {
                "spec": WORKFLOW_SPEC,
                "waitMs": {"type": "integer", "minimum": 1,
                           "description": "Maximum time, in milliseconds, that this call waits for the run to reach a terminal state. If the run is still going when the wait ends, the result carries waitExpired: true; then call get_workflow again with the same workflowId. A value above the relay's limit (A2A_RELAY_MAX_WAIT_MS, default 600000) is clamped to that limit, never raised: the result then carries waitMs (the wait used) and waitClamped: true. Use about 110000 or less where a tool call longer than 120 s moves to the background."},
            },
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
    },
    {
        "name": "get_workflow",
        "description": "Get a workflow run with every step, its status, attempts, output, and error. Pass maxWaitMs to long-poll until the run is terminal or the wait elapses; a result that is still running then carries waitExpired: true, and you call get_workflow again with the same workflowId. A run that has left the relay's memory but still has a journal on disk returns unknown_workflow with a message: continue it with resume_workflow.",
        "inputSchema": {
            "type": "object", "required": ["workflowId"], "additionalProperties": False,
            "properties": {
                "workflowId": {"type": "string", "minLength": 1, "maxLength": 128,
                               "description": "Run ID returned by run_workflow."},
                "maxWaitMs": {"type": "integer", "minimum": 1,
                              "description": "Maximum time, in milliseconds, to wait for the run to become terminal before returning its current state. A value above the relay's limit (A2A_RELAY_MAX_WAIT_MS, default 600000) is clamped to that limit, never raised: the result then carries waitMs (the wait used) and waitClamped: true. Use about 110000 or less where a tool call longer than 120 s moves to the background."},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "list_workflows",
        "description": "List stored workflow runs, most recent first, optionally filtered by session or status. Entries carry every step's status but omit the run's spec and each step's output; use get_workflow for one run in full. Use it to recover a workflowId after a context reset.",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "sessionId": {"type": "string", "minLength": 1, "maxLength": 128,
                              "description": "Filter to one session. Get the ID from a run_workflow result."},
                "status": {"type": "string", "enum": list(WORKFLOW_STATUSES),
                           "description": "Filter by workflow status."},
                "limit": {"type": "integer", "minimum": 1,
                          "description": "Maximum number of runs to return."},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "cancel_workflow",
        "description": "Cancel a workflow run and every task it has started. Use it when the user changes their mind or a step is clearly stuck. Terminal runs are returned unchanged.",
        "inputSchema": {
            "type": "object", "required": ["workflowId"], "additionalProperties": False,
            "properties": {"workflowId": {"type": "string", "minLength": 1, "maxLength": 128,
                                          "description": "Run ID returned by run_workflow."}},
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    },
    {
        "name": "resume_workflow",
        "description": "Continue a workflow run from its journal after a failed or interrupted attempt, keeping every step that already completed and re-running only the rest. Use it when get_workflow reports that a run is no longer active, or after fixing whatever made a step fail. Pass waitMs to block until the run finishes again.",
        "inputSchema": {
            "type": "object", "required": ["workflowId"], "additionalProperties": False,
            "properties": {
                "workflowId": {"type": "string", "minLength": 1, "maxLength": 128,
                               "description": "Run ID returned by run_workflow."},
                "waitMs": {"type": "integer", "minimum": 1,
                           "description": "Maximum time, in milliseconds, to wait for the resumed run to become terminal. A value above the relay's limit (A2A_RELAY_MAX_WAIT_MS, default 600000) is clamped to that limit, never raised: the result then carries waitMs (the wait used) and waitClamped: true. Use about 110000 or less where a tool call longer than 120 s moves to the background."},
            },
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
    },
    {
        "name": "get_pressure",
        "description": "Report how hard this machine is working: the kernel's memory pressure level and free percentage, swap in use, and any CPU thermal throttle. Call it before starting a long or multi-agent workflow: the relay refuses a new workflow while the verdict is `critical`, and its guard stops running workflows if the machine becomes critical mid-run. The verdict is `ok`, `warn` or `critical`, and `reasons` names the reading that decided it. A stopped run says `device_pressure:` at the start of its `error` and can be continued with resume_workflow once the machine recovers. `temperatureC` is null on Apple Silicon, because macOS exposes no unprivileged CPU temperature.",
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
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


def _refuses_wait_field(rejected):
    """True when `rejected` is a relay from before POST /v1/tasks took waitMs refusing that field."""
    data = rejected.data or {}
    return (rejected.status == 400 and data.get("error") == "unknown_field"
            and "waitMs" in str(data.get("message")))


def _health_wait_limit(base_url, wait_ms):
    """The relay's maxWaitMs from /healthz when it is a positive int below `wait_ms`, else None."""
    try:
        limit = (_relay_request(base_url, "/healthz").get("limits") or {}).get("maxWaitMs")
    except RelayError:
        return None
    return limit if _is_positive_int(limit) and limit < wait_ms else None


def _relay_wait_limit(base_url, rejected, wait_ms):
    """The relay's maxWaitMs, when `rejected` is its invalid_wait refusal of a wait above it.

    Returns None when the refusal is anything else, or when the relay reports no limit below
    `wait_ms`: the caller then reports the original error. Waiting longer than asked is wrong.
    """
    if rejected.status != 400 or (rejected.data or {}).get("error") != "invalid_wait":
        return None
    return _health_wait_limit(base_url, wait_ms)


def _is_string_map(value):
    return isinstance(value, dict) and all(
        isinstance(name, str) and name and isinstance(item, str) for name, item in value.items())


def _wait_workflow(base_url, workflow_id, wait_ms, timeout):
    """Long-poll a run, clamping the wait to the relay's limit when it refuses the first one."""
    path = "/v1/workflows/" + quote(workflow_id, safe="") + f"?waitMs={wait_ms}"
    try:
        return _relay_request(base_url, path, timeout=max(timeout, wait_ms / 1000.0 + 5))
    except RelayError as exc:
        limit = _relay_wait_limit(base_url, exc, wait_ms)
        if limit is None:
            raise
        path = "/v1/workflows/" + quote(workflow_id, safe="") + f"?waitMs={limit}"
        value = _relay_request(base_url, path, timeout=max(timeout, limit / 1000.0 + 5))
        value.update(waitMs=limit, waitClamped=True)
        return value


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


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _http_request(base_url, path, method="GET", body=None, timeout=10.0):
    from urllib import error as urlerror

    data = None
    headers = {}
    token = _token()
    if token:
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" and not _loopback(base_url):
            raise RelayError(None, {"error": "insecure_relay_url",
                                    "message": "refusing to send the API token over cleartext http "
                                               "to a non-loopback relay; use an https URL"},
                             "refusing to send the API token over cleartext http to a non-loopback relay")
        headers["authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    request = urlrequest.Request(base_url + path, data=data, method=method, headers=headers)
    # Never follow redirects: urllib would re-send the Authorization header to the new host.
    opener = urlrequest.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
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
    hostname = urlsplit(base_url).hostname or ""
    try:
        return hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


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
                "serverInfo": {"name": "agent-relay", "version": "0.3.0"},
                "instructions": (
                    "agent-relay sends tasks to other coding agents (pi, Codex, OpenCode, "
                    "Claude Code) and returns their results. When the user asks to delegate, "
                    "orchestrate, fan out, or use another agent or model: call list_agents, "
                    "then delegate, then wait_task. Do not run agent CLIs (pi, codex, "
                    "opencode, claude) in a shell to do this work. The relay owns task IDs, "
                    "status, timeouts, cancellation, and idempotency. For parallel work, call "
                    "delegate once per task, then wait for each taskId. Reuse the sessionId "
                    "from an earlier task to group related work for list_tasks; it does not "
                    "resume the harness session. Omit it to start a new session. If no listed "
                    "agent fits, tell the user and ask before you run a CLI directly."
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
                args = _arguments_for(rpc, ["agentId", "input", "sessionId", "requestId",
                                            "timeoutMs", "cwd", "options", "readOnly", "waitMs"])
                _non_empty(args, "agentId", 128)
                _non_empty(args, "input")
                if "sessionId" in args:
                    _non_empty(args, "sessionId", 128)
                if "requestId" in args:
                    _non_empty(args, "requestId", 128)
                if "timeoutMs" in args and not _is_positive_int(args["timeoutMs"]):
                    _invalid("timeoutMs must be a positive integer")
                if "cwd" in args:
                    _non_empty(args, "cwd")
                if "options" in args and not _is_string_map(args["options"]):
                    _invalid("options must be an object of option names to value strings")
                if "readOnly" in args and not isinstance(args["readOnly"], bool):
                    _invalid("readOnly must be true or false")
                if "waitMs" in args and not _is_positive_int(args["waitMs"]):
                    _invalid("waitMs must be a positive integer")
                payload = {"agentId": args["agentId"], "input": args["input"]}
                for key in ("sessionId", "requestId", "timeoutMs", "cwd", "options", "readOnly"):
                    if key in args:
                        payload[key] = args[key]
                # Set by the relay for agents it spawned: the relay then applies its
                # delegation policy (allowed targets, cycles, depth, budget) to this task.
                parent_task = _env("AGENT_RELAY_PARENT_TASK_ID")
                if parent_task:
                    payload["parentTaskId"] = parent_task
                # Send the wait with the submission: the relay refuses a wait it cannot honour
                # before it creates the task, so a refusal leaves no task running without an ID.
                wait_ms = args.get("waitMs")
                if wait_ms:
                    payload["waitMs"] = wait_ms
                clamped = False
                try:
                    value = _relay_request(base_url, "/v1/tasks", method="POST", body=payload, timeout=timeout)
                except RelayError as exc:
                    if not wait_ms:
                        raise
                    if _refuses_wait_field(exc):
                        del payload["waitMs"]  # a relay from before this field: no pre-check, still waits
                        # Its long poll still refuses a wait above its limit, so clamp to /healthz's.
                        limit = _health_wait_limit(base_url, wait_ms)
                        if limit is not None:
                            wait_ms, clamped = limit, True
                    else:
                        limit = _relay_wait_limit(base_url, exc, wait_ms)
                        if limit is None:
                            raise
                        wait_ms, clamped = limit, True
                        payload["waitMs"] = limit
                    # Nothing was created, so the retry cannot duplicate the task, even with a requestId.
                    value = _relay_request(base_url, "/v1/tasks", method="POST", body=payload, timeout=timeout)
                if wait_ms and value.get("status") in ("queued", "running"):
                    value = _relay_request(
                        base_url, f"/v1/tasks/{quote(value['id'], safe='')}?waitMs={wait_ms}",
                        timeout=max(timeout, wait_ms / 1000.0 + 5))
                if clamped:
                    value.update(waitMs=wait_ms, waitClamped=True)
            elif name == "wait_task":
                args = _arguments_for(rpc, ["taskId", "maxWaitMs"])
                _non_empty(args, "taskId", 128)
                max_wait = args.get("maxWaitMs", 60_000)
                if not _is_positive_int(max_wait):
                    _invalid("maxWaitMs must be a positive integer")
                task_path = "/v1/tasks/" + quote(args["taskId"], safe="")
                try:
                    value = _relay_request(base_url, f"{task_path}?waitMs={max_wait}",
                                           timeout=max(timeout, max_wait / 1000.0 + 5))
                except RelayError as exc:
                    limit = _relay_wait_limit(base_url, exc, max_wait)
                    if limit is None:
                        raise
                    value = _relay_request(base_url, f"{task_path}?waitMs={limit}",
                                           timeout=max(timeout, limit / 1000.0 + 5))
                    value.update(waitMs=limit, waitClamped=True)
            elif name == "get_task":
                args = _arguments_for(rpc, ["taskId", "includeInput"])
                _non_empty(args, "taskId", 128)
                if "includeInput" in args and not isinstance(args["includeInput"], bool):
                    _invalid("includeInput must be a boolean")
                path = "/v1/tasks/" + quote(args["taskId"], safe="")
                if args.get("includeInput"):
                    path += "?includeInput=true"
                value = _relay_request(base_url, path, timeout=timeout)
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
            elif name == "cancel_task":
                args = _arguments_for(rpc, ["taskId"])
                _non_empty(args, "taskId", 128)
                value = _relay_request(base_url, "/v1/tasks/" + quote(args["taskId"], safe=""),
                                       method="DELETE", timeout=timeout)
            elif name == "run_workflow":
                args = _arguments_for(rpc, ["spec", "waitMs"])
                spec = args.get("spec")
                if not isinstance(spec, dict):
                    _invalid("spec must be an object")
                if "waitMs" in args and not _is_positive_int(args["waitMs"]):
                    _invalid("waitMs must be a positive integer")
                value = _relay_request(base_url, "/v1/workflows", method="POST", body=spec,
                                       timeout=timeout)
                wait_ms = args.get("waitMs")
                if wait_ms and value.get("status") in ("queued", "running"):
                    value = _wait_workflow(base_url, value["id"], wait_ms, timeout)
            elif name == "get_workflow":
                args = _arguments_for(rpc, ["workflowId", "maxWaitMs"])
                _non_empty(args, "workflowId", 128)
                wait_ms = args.get("maxWaitMs")
                if wait_ms is None:
                    value = _relay_request(
                        base_url, "/v1/workflows/" + quote(args["workflowId"], safe=""),
                        timeout=timeout)
                elif not _is_positive_int(wait_ms):
                    _invalid("maxWaitMs must be a positive integer")
                else:
                    value = _wait_workflow(base_url, args["workflowId"], wait_ms, timeout)
            elif name == "list_workflows":
                args = _arguments_for(rpc, ["sessionId", "status", "limit"])
                params = {}
                if "sessionId" in args:
                    _non_empty(args, "sessionId", 128)
                    params["sessionId"] = args["sessionId"]
                if "status" in args:
                    if args["status"] not in WORKFLOW_STATUSES:
                        _invalid(f"status must be one of {', '.join(WORKFLOW_STATUSES)}")
                    params["status"] = args["status"]
                if "limit" in args:
                    if not _is_positive_int(args["limit"]):
                        _invalid("limit must be a positive integer")
                    params["limit"] = args["limit"]
                path = "/v1/workflows" + (f"?{urlencode(params)}" if params else "")
                value = _relay_request(base_url, path, timeout=timeout)
            elif name == "cancel_workflow":
                args = _arguments_for(rpc, ["workflowId"])
                _non_empty(args, "workflowId", 128)
                value = _relay_request(
                    base_url, "/v1/workflows/" + quote(args["workflowId"], safe=""),
                    method="DELETE", timeout=timeout)
            elif name == "resume_workflow":
                args = _arguments_for(rpc, ["workflowId", "waitMs"])
                _non_empty(args, "workflowId", 128)
                if "waitMs" in args and not _is_positive_int(args["waitMs"]):
                    _invalid("waitMs must be a positive integer")
                value = _relay_request(
                    base_url, "/v1/workflows/" + quote(args["workflowId"], safe="") + "/resume",
                    method="POST", timeout=timeout)
                wait_ms = args.get("waitMs")
                if wait_ms and value.get("status") in ("queued", "running"):
                    value = _wait_workflow(base_url, args["workflowId"], wait_ms, timeout)
            elif name == "get_pressure":
                _arguments_for(rpc, [])
                value = _relay_request(base_url, "/v1/pressure", timeout=timeout)
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
