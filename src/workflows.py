#!/usr/bin/env python3
"""Declarative workflow engine for agent-relay.

A workflow document names steps; each step runs as an ordinary relay task on one
registered agent. Steps are wired together with ``needs``, so the engine submits a step
only once every step it depends on has completed, and records each step's output for the
steps that follow::

    {"name": "ship-fix", "steps": [
        {"id": "write",  "agentId": "claude", "input": "Fix the failing test"},
        {"id": "review", "agentId": "codex",  "needs": ["write"], "input": "{{write.output}}"}
    ]}

Input strings are templates. ``{{stepId.output}}`` resolves against a completed
dependency, ``{{item}}`` and ``{{index}}`` against a ``foreach`` expansion. Steps may fan
out with ``foreach``, re-run on a task failure with ``retries``, loop until their
output matches a regular expression with ``repeat``, and fail when a task's own output
matches a regular expression with ``failIfOutputMatches`` — each item of a ``foreach``
step is matched on its own raw output, so one blocked item fails the whole step.

Every run is journaled to ``<state dir>/workflows/<id>.jsonl``. Completed steps therefore
survive a relay restart with their output and their ``startedAt`` / ``finishedAt`` window,
and ``resume`` continues a run without repeating finished work. Each step and run status
change is also appended to an in-memory, per-session change feed with a rising sequence
number, so a caller can wait for the next change of any run in one session instead of
polling each run.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

STEP_ID_RE = re.compile(r"^[A-Za-z0-9_.~-]{1,128}$")
RUN_ID_RE = re.compile(r"^wf-[0-9a-f]{32}$")
TEMPLATE_RE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
FOREACH_REFS = ("item", "index", "foreach.item", "foreach.index")

WORKFLOW_STATUSES = ("queued", "running", "completed", "failed", "cancelled")
WORKFLOW_TERMINAL = frozenset(("completed", "failed", "cancelled"))
STEP_TERMINAL = frozenset(("completed", "failed", "skipped", "cancelled"))
TASK_TERMINAL = frozenset(("completed", "failed", "timed_out", "cancelled"))

WORKFLOW_FIELDS = frozenset(("name", "sessionId", "requestId", "steps"))
STEP_FIELDS = frozenset(("id", "agentId", "input", "needs", "needsPolicy", "foreach", "timeoutMs",
                         "cwd", "options", "readOnly", "retries", "repeat", "failIfOutputMatches"))
REPEAT_FIELDS = frozenset(("max", "untilOutputMatches"))

MAX_REPEAT_ROUNDS = 10
MAX_RETRIES = 5
MAX_ITEM_CHARS = 1024
# The change feed is bounded; the oldest events fall off the back, so an ``after`` older
# than that still works and simply skips what is gone.
MAX_EVENT_FEED = 4096

# A failed step reports the cause, not the whole transcript: coding CLIs print a banner first
# and the reason they failed last, so only the tail of each stream belongs in the summary.
CAUSE_TAIL_LINES = 3
CAUSE_MAX_CHARS = 2000
QUOTA_CAUSE_RE = re.compile(r"usage limit|rate.?limit|quota|too many requests", re.IGNORECASE)


def _text(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit


def _tail_lines(text, keep):
    """The last ``keep`` non-empty lines of a stream, whitespace-trimmed."""
    return [line.strip() for line in (text or "").splitlines() if line.strip()][-keep:]


def _step_cause(stderr, output):
    """The short cause for a failed step: the last non-empty lines the agent printed.

    The whole transcript stays on the task and in the step's ``errorDetail``. A quota or
    rate-limit line names the cause ``agent_quota`` (with any stated retry time inside it),
    so a caller can wait or switch agents without reading the transcript.
    """
    lines = _tail_lines(stderr, CAUSE_TAIL_LINES) + _tail_lines(output, CAUSE_TAIL_LINES)
    kept = []
    for line in reversed(lines):
        if kept and sum(len(item) + 1 for item in kept) + len(line) > CAUSE_MAX_CHARS:
            break
        kept.append(line[:CAUSE_MAX_CHARS])
    cause = "\n".join(reversed(kept))
    if QUOTA_CAUSE_RE.search(cause):
        return f"agent_quota: {cause}"
    return cause


def _dependency_order(steps):
    """Return the step ids that still depend on something, empty when acyclic."""
    remaining = {step["id"]: set(step["needs"]) for step in steps}
    resolved = set()
    progressed = True
    while progressed:
        progressed = False
        for step_id, deps in list(remaining.items()):
            if deps <= resolved:
                resolved.add(step_id)
                del remaining[step_id]
                progressed = True
    return sorted(remaining)


def _ancestors(steps):
    """Map each step id to the transitive closure of the steps it depends on."""
    deps = {step["id"]: list(step["needs"]) for step in steps}
    memo = {}

    def walk(step_id):
        if step_id in memo:
            return memo[step_id]
        found = set()
        for dep in deps[step_id]:
            found.add(dep)
            found.update(walk(dep))
        memo[step_id] = frozenset(found)
        return memo[step_id]

    # Plain lists: the closure is journaled with the spec, so it has to survive JSON.
    return {step_id: list(walk(step_id)) for step_id in deps}


def _check_reference(reference, has_foreach, ancestors, known_ids):
    """Return a problem description for a template reference, or None when it is sound."""
    if reference in FOREACH_REFS:
        if has_foreach:
            return None
        if reference not in known_ids:  # a step may itself be called "item"
            return f"{{{{{reference}}}}} is only valid on a step that has foreach"
    # A step id may contain dots, so only a trailing ".output" is a suffix.
    step_id = reference[:-len(".output")] if reference.endswith(".output") else reference
    if step_id not in known_ids:
        return f"{{{{{reference}}}}} does not name a step in this workflow"
    if step_id not in ancestors:
        return (f"{{{{{reference}}}}} must name a step listed in needs, directly or "
                f"through another step")
    return None


def validate_spec(spec, agents, max_steps, max_foreach, max_body, max_command_input,
                  max_timeout_ms):
    """Check a workflow document.

    Returns ``(normalized_spec, None)`` when the document is usable, otherwise
    ``(None, (error_code, message))``.
    """
    def bad(code, message):
        return None, (code, message)

    if not isinstance(spec, dict):
        return bad("invalid_request", "A workflow must be a JSON object")
    unknown = sorted(set(spec) - WORKFLOW_FIELDS)
    if unknown:
        return bad("unknown_field", f"Unknown workflow field: {unknown[0]}")
    name = spec.get("name")
    if name is not None and not _text(name, 128):
        return bad("invalid_name", "name must be a non-empty string of at most 128 characters")
    session_id = spec.get("sessionId")
    if session_id is not None and not _text(session_id, 128):
        return bad("invalid_session_id",
                   "sessionId must be a non-empty string of at most 128 characters")
    request_id = spec.get("requestId")
    if request_id is not None and not _text(request_id, 128):
        return bad("invalid_request_id",
                   "requestId must be a non-empty string of at most 128 characters")

    raw_steps = spec.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return bad("invalid_steps", "steps must be a non-empty array")
    if len(raw_steps) > max_steps:
        return bad("invalid_steps", f"steps exceeds the limit of {max_steps}")

    steps = []
    seen = set()
    for position, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            return bad("invalid_steps", f"steps[{position}] must be an object")
        unknown = sorted(set(raw) - STEP_FIELDS)
        if unknown:
            return bad("unknown_field", f"Unknown step field: {unknown[0]}")

        step_id = raw.get("id")
        if not isinstance(step_id, str) or not STEP_ID_RE.fullmatch(step_id):
            return bad("invalid_step_id",
                       f"steps[{position}].id must match [A-Za-z0-9_.~-] and be at most 128 characters")
        if step_id in seen:
            return bad("duplicate_step", f"Duplicate step id: {step_id}")
        seen.add(step_id)

        agent_id = raw.get("agentId")
        if not isinstance(agent_id, str) or agent_id not in agents:
            return bad("unknown_agent",
                       f"Step {step_id} names an agent that is not registered: {agent_id}")
        agent = agents[agent_id]

        text = raw.get("input")
        if not _text(text, max_body):
            return bad("input_required",
                       f"Step {step_id} needs a non-empty input of at most {max_body} characters")
        if agent["type"] == "command" and len(text.encode("utf-8")) > max_command_input:
            return bad("command_input_too_large",
                       f"Step {step_id} input exceeds A2A_RELAY_MAX_COMMAND_INPUT_BYTES for a "
                       f"command adapter; register a stdio adapter for large prompts")

        needs = raw.get("needs", [])
        if not isinstance(needs, list) or not all(isinstance(dep, str) for dep in needs):
            return bad("invalid_needs", f"Step {step_id}.needs must be an array of step ids")
        if len(needs) != len(set(needs)):
            return bad("invalid_needs", f"Step {step_id}.needs repeats a step id")
        if step_id in needs:
            return bad("cyclic_dependency", f"Step {step_id} cannot depend on itself")
        needs_policy = raw.get("needsPolicy")
        if needs_policy is not None and needs_policy not in ("any", "all"):
            return bad("invalid_needs_policy",
                       f"Step {step_id}.needsPolicy must be \"any\" or \"all\"")
        if needs_policy == "any" and not needs:
            return bad("invalid_needs_policy",
                       f"Step {step_id} cannot set needsPolicy \"any\" without needs")

        foreach = raw.get("foreach")
        if foreach is not None:
            if not isinstance(foreach, list) or not foreach:
                return bad("invalid_foreach",
                           f"Step {step_id}.foreach must be a non-empty array")
            if len(foreach) > max_foreach:
                return bad("invalid_foreach",
                           f"Step {step_id}.foreach exceeds the limit of {max_foreach} items")
            if not all(isinstance(item, str) and item and len(item) <= MAX_ITEM_CHARS
                       for item in foreach):
                return bad("invalid_foreach",
                           f"Step {step_id}.foreach items must be non-empty strings of at most "
                           f"{MAX_ITEM_CHARS} characters")

        timeout = raw.get("timeoutMs")
        if timeout is not None:
            too_big = max_timeout_ms and isinstance(timeout, int) and timeout > max_timeout_ms
            if (isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
                    or too_big):
                suffix = f" of at most {max_timeout_ms}" if max_timeout_ms else ""
                return bad("invalid_timeout",
                           f"Step {step_id}.timeoutMs must be a positive integer{suffix}")

        cwd = raw.get("cwd")
        if cwd is not None and (not isinstance(cwd, str) or not cwd):
            return bad("invalid_cwd", f"Step {step_id}.cwd must be a non-empty string")

        # The relay is the authority on whether this agent can honour these; the shape is checked
        # here so an obvious mistake is a 400 rather than a step that fails when it is launched.
        options = raw.get("options")
        if options is not None and (not isinstance(options, dict) or not all(
                isinstance(name, str) and name and isinstance(value, str)
                for name, value in options.items())):
            return bad("invalid_options",
                       f"Step {step_id}.options must be an object of option names to value "
                       f"strings; list_agents shows what each agent allows")
        read_only = raw.get("readOnly", False)
        if not isinstance(read_only, bool):
            return bad("invalid_read_only",
                       f"Step {step_id}.readOnly must be true or false")

        retries = raw.get("retries", 0)
        if (isinstance(retries, bool) or not isinstance(retries, int)
                or not 0 <= retries <= MAX_RETRIES):
            return bad("invalid_retries",
                       f"Step {step_id}.retries must be an integer between 0 and {MAX_RETRIES}")

        fail_pattern = raw.get("failIfOutputMatches")
        if fail_pattern is not None:
            if not isinstance(fail_pattern, str) or not fail_pattern:
                return bad("invalid_fail_if",
                           f"Step {step_id}.failIfOutputMatches must be a non-empty regular "
                           f"expression")
            try:
                re.compile(fail_pattern)
            except re.error as exc:
                return bad("invalid_fail_if",
                           f"Step {step_id}.failIfOutputMatches is not a valid regular "
                           f"expression: {exc}")

        repeat = raw.get("repeat")
        repeat_value = None
        if repeat is not None:
            if not isinstance(repeat, dict):
                return bad("invalid_repeat", f"Step {step_id}.repeat must be an object")
            unknown = sorted(set(repeat) - REPEAT_FIELDS)
            if unknown:
                return bad("unknown_field", f"Unknown repeat field: {unknown[0]}")
            rounds = repeat.get("max")
            if (isinstance(rounds, bool) or not isinstance(rounds, int)
                    or not 1 <= rounds <= MAX_REPEAT_ROUNDS):
                return bad("invalid_repeat",
                           f"Step {step_id}.repeat.max must be an integer between 1 and "
                           f"{MAX_REPEAT_ROUNDS}")
            pattern = repeat.get("untilOutputMatches")
            if not isinstance(pattern, str) or not pattern:
                return bad("invalid_repeat",
                           f"Step {step_id}.repeat.untilOutputMatches must be a non-empty "
                           f"regular expression")
            try:
                re.compile(pattern)
            except re.error as exc:
                return bad("invalid_repeat",
                           f"Step {step_id}.repeat.untilOutputMatches is not a valid regular "
                           f"expression: {exc}")
            if foreach is not None:
                return bad("invalid_repeat",
                           f"Step {step_id} cannot combine repeat with foreach; repeat a single "
                           f"task, fan out with foreach")
            repeat_value = {"max": rounds, "untilOutputMatches": pattern}

        steps.append({
            "id": step_id,
            "agentId": agent_id,
            "input": text,
            "needs": list(needs),
            "needsPolicy": needs_policy,
            "foreach": list(foreach) if foreach is not None else None,
            "timeoutMs": timeout,
            "cwd": cwd,
            "options": dict(options) if options is not None else None,
            "readOnly": read_only,
            "retries": retries,
            "repeat": repeat_value,
            "failIfOutputMatches": fail_pattern,
        })

    known_ids = {step["id"] for step in steps}
    for step in steps:
        for dep in step["needs"]:
            if dep not in known_ids:
                return bad("unknown_step",
                           f"Step {step['id']} needs {dep}, which is not a step in this workflow")
    cyclic = _dependency_order(steps)
    if cyclic:
        return bad("cyclic_dependency",
                   "Workflow steps form a dependency cycle: " + ", ".join(cyclic))

    ancestors = _ancestors(steps)
    for step in steps:
        text = step["input"]
        remainder = TEMPLATE_RE.sub("", text)
        if "{{" in remainder or "}}" in remainder:
            hint = "use {{ stepId.output }} or {{ item }}"
            return bad("invalid_template",
                       f"Step {step['id']} input has a malformed placeholder; {hint}")
        for match in TEMPLATE_RE.finditer(text):
            problem = _check_reference(match.group(1), step["foreach"] is not None,
                                       ancestors[step["id"]], known_ids)
            if problem:
                return bad("unknown_reference", f"Step {step['id']}: {problem}")

    normalized = {
        "steps": steps,
        "ancestors": ancestors,
    }
    if name is not None:
        normalized["name"] = name
    if session_id is not None:
        normalized["sessionId"] = session_id
    if request_id is not None:
        normalized["requestId"] = request_id
    return normalized, None


def _render(text, context):
    def replace(match):
        value = context.get(match.group(1))
        if value is None:
            raise KeyError(match.group(1))
        return value

    return TEMPLATE_RE.sub(replace, text)


class WorkflowEngine:
    """Schedules workflow steps onto the relay's task queue and records their results."""

    def __init__(self, *, lock, condition, create_task, get_task, cancel_task, drain,
                 agents, resolve_cwd, state_dir, max_steps, max_workflows, max_foreach,
                 max_body, drain_refusal=None,
                 max_command_input, max_timeout_ms, now):
        self._lock = lock
        self._condition = condition
        self._create_task = create_task
        self._get_task = get_task
        self._cancel_task = cancel_task
        self._drain = drain
        # Asked under the lock on the create path only, so a held shared-lock
        # drain blocks new runs and resumes atomically and never a replay.
        self._drain_refusal = drain_refusal
        self._agents = agents
        self._resolve_cwd = resolve_cwd
        self._state_dir = Path(state_dir)
        self._max_steps = max_steps
        self._max_workflows = max_workflows
        self._max_foreach = max_foreach
        self._max_body = max_body
        self._max_command_input = max_command_input
        self._max_timeout_ms = max_timeout_ms
        self._now = now
        self._workflows = {}
        self._request_ids = {}
        self._events = []
        self._seq = 0

    # ------------------------------------------------------------------ public API

    def submit(self, spec, refusal=None):
        """Validate and start a workflow. Returns ``(http_status, body)``.

        ``refusal`` is asked once per submission whether the host can take a new run. Its non-None
        answer is applied only when the submission would create a run, so a replay of a run that
        already exists still gets that run back instead of a refusal that would lose its id; a
        replay costs one reading.
        """
        agents = self._agents()
        normalized, problem = validate_spec(
            spec, agents, self._max_steps, self._max_foreach, self._max_body,
            self._max_command_input, self._max_timeout_ms)
        if problem:
            code, message = problem
            return 400, {"error": code, "message": message}
        for step in normalized["steps"]:
            if step["cwd"] is None:
                continue
            resolved, error = self._resolve_cwd(agents[step["agentId"]], step["cwd"])
            if error:
                message = (f"Step {step['id']}: cwd is not inside an allowed root for agent "
                           f"{step['agentId']}" if error == "cwd_not_allowed"
                           else f"Step {step['id']}: cwd is not a directory: {step['cwd']}")
                return 400, {"error": error, "message": message}
            step["cwd"] = resolved
        request_id = normalized.pop("requestId", None)
        # Asked before the lock is taken so a slow probe never blocks the scheduler; the answer is
        # applied below only on the path that creates a run.
        refused = refusal() if refusal is not None else None

        with self._lock:
            if request_id is not None:
                prior = self._request_ids.get(request_id)
                if prior is not None:
                    if prior["_requested_spec"] != normalized:
                        return 409, {"error": "idempotency_conflict"}
                    return 200, self._public(prior, detail=False)
            # The shared-lock drain closes admission entirely, starting with
            # new runs: asked here, under the lock, so the refusal and the
            # registration of a new run cannot interleave with an acquire.
            refused_by_drain = self._drain_refusal() if self._drain_refusal else None
            if refused_by_drain:
                return refused_by_drain
            if refused:
                return 503, refused
            if not self._make_room():
                return 503, {"error": "workflow_capacity_reached"}
            workflow = self._new_workflow(normalized, request_id)
            self._workflows[workflow["id"]] = workflow
            if request_id is not None:
                self._request_ids[request_id] = workflow
            self._journal(workflow, {"type": "created", "at": self._now(),
                                     "spec": normalized})
            body = self._public(workflow, detail=False)
            self._start(workflow)
        return 202, body

    def _make_room(self):
        """Evict the oldest terminal run so the store stays bounded. Call with the lock held."""
        if len(self._workflows) < self._max_workflows:
            return True
        finished = [run for run in self._workflows.values()
                    if run["status"] in WORKFLOW_TERMINAL]
        if not finished:
            return False
        oldest = min(finished, key=lambda run: run["finishedAt"] or run["createdAt"])
        del self._workflows[oldest["id"]]
        self._request_ids.pop(oldest["_requested_request_id"], None)
        return True

    def get(self, workflow_id):
        if not RUN_ID_RE.fullmatch(workflow_id or ""):
            return None
        with self._lock:
            return self._workflows.get(workflow_id)

    def journaled(self, workflow_id):
        """True when a run with this id has a journal on disk, active run or not."""
        if not RUN_ID_RE.fullmatch(workflow_id or ""):
            return False
        return self._journal_path(workflow_id).exists()

    def public(self, workflow_id, detail=True, view=None, step=None):
        """The run as clients see it. ``view="summary"`` projects each step down to its
        identity, status, times and output size plus first line; ``step`` narrows the steps
        to that one id with its full output and errorDetail. Both leave out the spec, and
        either returns None when the run (or the step) is unknown."""
        if not RUN_ID_RE.fullmatch(workflow_id or ""):
            return None
        with self._lock:
            workflow = self._workflows.get(workflow_id)
            return self._public(workflow, detail=detail, view=view, step=step) if workflow else None

    def list(self, session_id=None, status=None, limit=100):
        with self._lock:
            workflows = list(self._workflows.values())
        if session_id is not None:
            workflows = [wf for wf in workflows if wf["sessionId"] == session_id]
        if status is not None:
            workflows = [wf for wf in workflows if wf["status"] == status]
        workflows.sort(key=lambda wf: wf["createdAt"], reverse=True)
        return [self._public(wf, detail=False) for wf in workflows[:limit]]

    def cancel(self, workflow_id):
        """Stop a workflow and its running children. Returns the workflow, or None."""
        workflow = self.get(workflow_id)
        if workflow is None:
            return None
        with self._lock:
            if workflow["status"] not in WORKFLOW_TERMINAL:
                # Finish here rather than on the next tick: the caller is waiting for
                # this response and must not see a run that still reads "queued".
                workflow["_cancel"] = True
                self._cancel_children(workflow)
                self._finish(workflow, "cancelled")
        return self._public(workflow, detail=True)

    def active_ids(self):
        """The ids of runs that have not reached a terminal state."""
        with self._lock:
            return [workflow_id for workflow_id, workflow in self._workflows.items()
                    if workflow["status"] not in WORKFLOW_TERMINAL]

    def active_count(self):
        """How many runs have not reached a terminal state: the drain's own count
        of nonterminal workflows. Runs in a gap, a retry delay or mid fanout all
        count, whatever their child tasks' states, because a held drain must
        wait for an idle run that may still create tasks."""
        with self._lock:
            return sum(1 for workflow in self._workflows.values()
                       if workflow["status"] not in WORKFLOW_TERMINAL)

    def kill_all(self, reason):
        """Stop every active run, keeping each journal so it can be resumed. Returns the ids.

        This is the pressure guard's stop: a step that finished stays finished, so a resume
        after the machine recovers repeats only the step that was running.
        """
        killed = []
        with self._lock:
            for workflow_id, workflow in list(self._workflows.items()):
                if workflow["status"] in WORKFLOW_TERMINAL:
                    continue
                workflow["_cancel"] = True
                workflow["error"] = reason
                self._cancel_children(workflow)
                self._finish(workflow, "cancelled")
                killed.append(workflow_id)
        return killed

    def resume(self, workflow_id):
        """Continue a journaled run, keeping the steps that already completed."""
        if not RUN_ID_RE.fullmatch(workflow_id or ""):
            return 404, {"error": "unknown_workflow"}
        saved = self._read_journal(workflow_id)
        if saved is None:
            return 404, {"error": "unknown_workflow",
                         "message": "No journaled workflow with that id"}
        with self._lock:
            prior = self._workflows.get(workflow_id)
            if prior is not None and prior["status"] not in WORKFLOW_TERMINAL:
                return 409, {"error": "workflow_active",
                             "message": "The workflow is still running; cancel it before resuming"}
            # A resume creates a live run, so a held drain refuses it exactly
            # like a new submission, under this same lock.
            refused = self._drain_refusal() if self._drain_refusal else None
            if refused:
                return refused
            workflow = self._new_workflow(saved["spec"], None, workflow_id=workflow_id)
            for step in workflow["steps"]:
                done = saved["outputs"].get(step["id"])
                if done is not None:
                    step["status"] = "completed"
                    step["output"] = done["output"]
                    step["attempts"] = done["attempts"]
                    step["startedAt"] = done["startedAt"]
                    step["finishedAt"] = done["finishedAt"]
                    step["attemptTimes"] = done["attemptTimes"]
            self._workflows[workflow_id] = workflow
            # The one field _new_workflow sets and _make_room releases; reading the old
            # name left every resumed run unregistered.
            request_id = (prior or {}).get("_requested_request_id")
            if request_id is not None:
                workflow["_requested_request_id"] = request_id
                self._request_ids[request_id] = workflow
            self._journal(workflow, {"type": "resumed", "at": self._now()})
            body = self._public(workflow, detail=False)
            self._start(workflow)
        return 202, body

    def wait(self, workflow_id, wait_ms):
        """Long-poll until the workflow is terminal or the wait elapses."""
        deadline = time.monotonic() + wait_ms / 1000.0
        with self._condition:
            while True:
                workflow = self._workflows.get(workflow_id)
                if workflow is None or workflow["status"] in WORKFLOW_TERMINAL:
                    return workflow
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return workflow
                self._condition.wait(remaining)

    def wait_events(self, session_id, after, wait_ms):
        """Long-poll for the session's changes after seq ``after``; ``[]`` when the wait elapses."""
        deadline = time.monotonic() + wait_ms / 1000.0
        with self._condition:
            while True:
                events = [event for event in self._events
                          if event["sessionId"] == session_id and event["seq"] > after]
                if events:
                    return events
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._condition.wait(remaining)

    # -------------------------------------------------------------- run internals

    def _new_workflow(self, spec, request_id, workflow_id=None):
        workflow_id = workflow_id or "wf-" + uuid.uuid4().hex
        session_id = spec.get("sessionId") or workflow_id
        return {
            "id": workflow_id,
            "name": spec.get("name") or "workflow",
            "sessionId": session_id,
            "status": "queued",
            "createdAt": self._now(),
            "startedAt": None,
            "finishedAt": None,
            "error": None,
            "spec": spec,
            "steps": [self._new_step(step) for step in spec["steps"]],
            "_ancestors": spec["ancestors"],
            "_requested_spec": spec,
            "_requested_request_id": request_id,
            "_cancel": False,
        }

    @staticmethod
    def _new_step(step):
        return {
            "id": step["id"],
            "agentId": step["agentId"],
            "needs": step["needs"],
            "status": "pending",
            "attempts": 0,
            "retries": 0,
            "startedAt": None,
            "finishedAt": None,
            "attemptTimes": [],
            "taskIds": [],
            "output": None,
            "error": None,
            "errorDetail": None,
            "_spec": step,
            "_current": [],
        }

    def _start(self, workflow):
        thread = threading.Thread(target=self._executor, args=(workflow,), daemon=True)
        thread.start()

    def _executor(self, workflow):
        while True:
            with self._lock:
                if self._workflows.get(workflow["id"]) is not workflow:
                    return
            try:
                if self._tick(workflow):
                    return
            except Exception as exc:  # noqa: BLE001 - a broken run must not kill the relay
                with self._lock:
                    workflow["status"] = "failed"
                    workflow["error"] = f"workflow engine error: {exc}"
                    workflow["finishedAt"] = self._now()
                    self._journal(workflow, {"type": "finished", "at": self._now(),
                                             "status": "failed"})
                    self._record_events(workflow)
                    self._condition.notify_all()
                return
            with self._condition:
                if workflow["status"] in WORKFLOW_TERMINAL:
                    return
                self._condition.wait(0.25)

    def _tick(self, workflow):
        """Advance one scheduling pass. Returns True once the run is terminal."""
        launched = False
        with self._lock:
            if workflow["status"] in WORKFLOW_TERMINAL:
                return True
            if not workflow["startedAt"]:
                workflow["startedAt"] = self._now()
            workflow["status"] = "running"

            if workflow["_cancel"]:
                self._cancel_children(workflow)
                self._finish(workflow, "cancelled")
                return True

            by_id = {step["id"]: step for step in workflow["steps"]}
            passes = 0
            changed = True
            while changed and passes <= len(workflow["steps"]) + 2:
                changed = False
                passes += 1
                for step in workflow["steps"]:
                    if step["status"] in STEP_TERMINAL:
                        continue
                    if step["status"] == "pending":
                        if self._release(workflow, step, by_id):
                            changed = True
                    if step["status"] == "ready":
                        if self._launch(workflow, step):
                            changed = True
                            launched = True
                    elif step["status"] == "running":
                        if self._reap(workflow, step):
                            changed = True
                            launched = True

            if all(step["status"] in STEP_TERMINAL for step in workflow["steps"]):
                statuses = [step["status"] for step in workflow["steps"]]
                if "cancelled" in statuses:
                    self._finish(workflow, "cancelled")
                elif "failed" in statuses or "skipped" in statuses:
                    culprit = next(step for step in workflow["steps"]
                                   if step["status"] in ("failed", "skipped"))
                    message = f"step {culprit['id']} {culprit['status']}"
                    if culprit["status"] == "failed" and culprit.get("error"):
                        # The run's own error carries the cause, so the summary a caller sees
                        # first is the last line the agent printed, not just the step id.
                        message = f"{message}: {culprit['error']}"
                    workflow["error"] = message
                    self._finish(workflow, "failed")
                else:
                    self._finish(workflow, "completed")
                return True
            self._record_events(workflow)
            self._condition.notify_all()
        if launched:
            self._drain()
        return False

    def _release(self, workflow, step, by_id):
        """Move a step from pending to ready, or skip it when a dependency failed."""
        dependencies = [by_id[dep] for dep in step["_spec"]["needs"]]
        required = "any" if step["_spec"].get("needsPolicy") == "any" else "all"
        if required == "any":
            # A fan-in that tolerates a failed branch: wait until every dependency is
            # terminal, then run when at least one completed and skip only when none did.
            if not all(dep["status"] in STEP_TERMINAL for dep in dependencies):
                return False
            if any(dep["status"] == "completed" for dep in dependencies):
                step["status"] = "ready"
                return True
        else:
            if any(dep["status"] in ("failed", "skipped", "cancelled") for dep in dependencies):
                step["status"] = "skipped"
                step["error"] = "dependency_not_completed"
                return True
            if all(dep["status"] == "completed" for dep in dependencies):
                step["status"] = "ready"
                return True
        if all(dep["status"] in STEP_TERMINAL for dep in dependencies):
            step["status"] = "skipped"
            step["error"] = "dependency_not_completed"
            return True
        return False

    def _context(self, workflow, step, item, index):
        outputs = {other["id"]: other["output"] for other in workflow["steps"]
                   if other["status"] == "completed"}
        context = {}
        tolerate = step["_spec"].get("needsPolicy") == "any"
        for dep in workflow["_ancestors"][step["id"]]:
            value = outputs.get(dep) or ""
            if tolerate and not value:
                # A dependency that did not complete still has one thing worth telling the
                # agent: what became of it, so the prompt names the failure, not silence.
                other = workflow["steps"][next(i for i, s in enumerate(workflow["steps"])
                                               if s["id"] == dep)]
                if other["status"] in ("failed", "timed_out", "skipped", "cancelled"):
                    error = (other.get("error") or other["status"])[:200]
                    value = f"[{dep} did not complete: status={other['status']} error={error}]"
            context[dep] = value
            context[f"{dep}.output"] = value
        if step["_spec"]["foreach"] is not None:
            context["item"] = item
            context["index"] = str(index)
            context["foreach.item"] = item
            context["foreach.index"] = str(index)
        return context

    def _end_attempt(self, step, now):
        """Close the window of the attempt that just ended."""
        windows = step["attemptTimes"]
        if windows and windows[-1]["finishedAt"] is None:
            windows[-1]["finishedAt"] = now

    def _settle(self, step):
        """Stamp a terminal step: close its last attempt and record when it ended."""
        if step["finishedAt"] is not None:
            return
        now = self._now()
        self._end_attempt(step, now)
        if step["startedAt"] is not None:
            step["finishedAt"] = now

    def _launch(self, workflow, step):
        """Render and submit every task this attempt needs."""
        spec = step["_spec"]
        now = self._now()
        if step["startedAt"] is None:
            step["startedAt"] = now
        items = spec["foreach"] if spec["foreach"] is not None else [None]
        submitted = []
        attempt = step["attempts"] + 1
        for index, item in enumerate(items):
            try:
                text = _render(spec["input"],
                               self._context(workflow, step, item, index))
            except KeyError as exc:
                self._abort_children(submitted)
                step["status"] = "failed"
                step["error"] = f"unresolved template reference: {exc.args[0]}"
                self._settle(step)
                return True
            request = {"agentId": spec["agentId"], "input": text,
                       "sessionId": workflow["sessionId"]}
            if spec["timeoutMs"] is not None:
                request["timeoutMs"] = spec["timeoutMs"]
            if spec["cwd"] is not None:
                request["cwd"] = spec["cwd"]
            if spec["options"] is not None:
                request["options"] = spec["options"]
            if spec["readOnly"]:
                request["readOnly"] = True
            status, body = self._create_task(request)
            if status not in (200, 202):
                self._abort_children(submitted)
                step["status"] = "failed"
                # Keep the relay's code: a document the agent cannot honour must be fixable
                # without guessing which rule refused it.
                code = body.get("error")
                message = body.get("message") or code or "task_rejected"
                step["error"] = f"{code}: {message}" if code and code not in message else message
                self._settle(step)
                return True
            submitted.append(body["id"])
        step["attempts"] = attempt
        step["attemptTimes"].append({"startedAt": now, "finishedAt": None})
        step["_current"] = submitted
        step["taskIds"].extend(submitted)
        step["status"] = "running"
        return True

    def _reap(self, workflow, step):
        """Fold finished tasks back into the step's state machine."""
        tasks = [self._get_task(task_id) for task_id in step["_current"]]
        if not tasks or any(task is None for task in tasks):
            step["status"] = "failed"
            step["error"] = "task_evicted_from_the_relay_store"
            self._settle(step)
            return True
        if not all(task["status"] in TASK_TERMINAL for task in tasks):
            return False
        spec = step["_spec"]

        failures = [task for task in tasks if task["status"] in ("failed", "timed_out")]
        if failures:
            if spec["retries"] and step["retries"] < spec["retries"]:
                step["retries"] += 1
                step["_current"] = []
                step["status"] = "ready"
                self._end_attempt(step, self._now())
                return True
            failed = failures[0]
            stderr = failed.get("error") or ""
            cause = _step_cause(stderr, failed.get("output") or "")
            step["status"] = "failed"
            step["error"] = cause or stderr or failed["status"]
            # The full transcript stays available on request (a single-run view), never in
            # the step error a summary or an event carries.
            step["errorDetail"] = stderr or None
            step["output"] = failed.get("output") or None
            self._settle(step)
            return True

        cancelled = [task for task in tasks if task["status"] == "cancelled"]
        if cancelled:
            step["status"] = "cancelled"
            step["error"] = "task_cancelled"
            self._settle(step)
            return True

        outputs = [task.get("output") or "" for task in tasks]
        step["output"] = json.dumps(outputs) if spec["foreach"] is not None else outputs[0]
        step["error"] = None

        # The CLI exited 0 but the agent's own report says it failed; a dependent would
        # otherwise run on a blocked result. Every task's raw output is matched on its own,
        # so a foreach step fails for one blocked item while a pattern only the JSON array
        # could satisfy (``^\[``) never fires on the serialization alone. A journal written
        # before this field existed resumes with the check absent, not with a KeyError.
        fail_pattern = spec.get("failIfOutputMatches")
        if fail_pattern is not None and any(re.search(fail_pattern, output)
                                            for output in outputs):
            step["status"] = "failed"
            step["error"] = "output_matched_fail_pattern"
            self._settle(step)
            return True

        if spec["repeat"] is not None:
            pattern = spec["repeat"]["untilOutputMatches"]
            if re.search(pattern, step["output"]) is None:
                if step["attempts"] < spec["repeat"]["max"]:
                    step["_current"] = []
                    step["status"] = "ready"
                    self._end_attempt(step, self._now())
                    return True
                step["status"] = "failed"
                step["error"] = "repeat_limit_reached"
                self._settle(step)
                return True

        step["status"] = "completed"
        self._settle(step)
        self._journal(workflow, {"type": "step", "at": self._now(), "step": step["id"],
                                 "status": "completed", "output": step["output"],
                                 "attempts": step["attempts"],
                                 "startedAt": step["startedAt"],
                                 "finishedAt": step["finishedAt"],
                                 "attemptTimes": step["attemptTimes"]})
        return True

    def _cancel_children(self, workflow):
        for step in workflow["steps"]:
            if step["status"] in STEP_TERMINAL:
                continue
            self._abort_children(step["_current"])
            step["status"] = "cancelled"
            step["error"] = "workflow_cancelled"
            self._settle(step)
            step["_current"] = []

    def _abort_children(self, task_ids):
        for task_id in task_ids:
            task = self._get_task(task_id)
            if task is not None and task["status"] not in TASK_TERMINAL:
                self._cancel_task(task)

    def _finish(self, workflow, status):
        workflow["status"] = status
        workflow["finishedAt"] = self._now()
        record = {"type": "finished", "at": self._now(), "status": status}
        if workflow["error"]:
            record["error"] = workflow["error"]
        self._journal(workflow, record)
        self._record_events(workflow)
        self._condition.notify_all()

    def _record_events(self, workflow):
        """Append one event per step or run status change since the last record. Lock held.

        Events carry the public status (a step waiting on the queue reads ``pending``, as in a
        run view), the run, the step (``None`` for a run-level change), and a seq that rises
        across the relay, so one ordered feed per session replaces polling each run.
        """
        changes = []
        run_status = workflow["status"]
        last_run = workflow.get("_event_run_status", "queued")
        if run_status != last_run:
            changes.append((None, last_run, run_status))
            workflow["_event_run_status"] = run_status
        for step in workflow["steps"]:
            status = "pending" if step["status"] == "ready" else step["status"]
            last = step.get("_event_status", "pending")
            if status != last:
                changes.append((step["id"], last, status))
                step["_event_status"] = status
        if not changes:
            return
        for step_id, old_status, new_status in changes:
            self._seq += 1
            self._events.append({"seq": self._seq, "sessionId": workflow["sessionId"],
                                 "workflowId": workflow["id"], "stepId": step_id,
                                 "oldStatus": old_status, "newStatus": new_status})
        if len(self._events) > MAX_EVENT_FEED:
            del self._events[:len(self._events) - MAX_EVENT_FEED]
        self._condition.notify_all()

    # ------------------------------------------------------------------- journaling

    def _journal_path(self, workflow_id):
        return self._state_dir / "workflows" / f"{workflow_id}.jsonl"

    def _journal(self, workflow, record):
        """Append one record. Journaling is best effort: it must never fail a run."""
        if workflow.get("_journal_off"):
            return
        try:
            path = self._journal_path(workflow["id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 - an unwritable state dir only costs resume
            workflow["_journal_off"] = True

    def _read_journal(self, workflow_id):
        try:
            lines = self._journal_path(workflow_id).read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        spec = None
        outputs = {}
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            kind = record.get("type")
            if kind == "created":
                spec = record.get("spec")
            elif kind == "step" and record.get("status") == "completed":
                # Last write wins: a resumed run may journal the same step again.
                outputs[record.get("step")] = {
                    "output": record.get("output") or "",
                    "attempts": record.get("attempts") or 1,
                    "startedAt": record.get("startedAt"),
                    "finishedAt": record.get("finishedAt"),
                    "attemptTimes": record.get("attemptTimes") or [],
                }
        if not isinstance(spec, dict) or "ancestors" not in spec:
            return None
        return {"spec": spec, "outputs": outputs}

    # ------------------------------------------------------------------- presentation

    def _public(self, workflow, detail, view=None, step=None):
        if workflow is None:
            return None
        steps = workflow["steps"]
        if step is not None:
            steps = [item for item in steps if item["id"] == step]
            if not steps:
                return None
        summary = view == "summary"
        snapshot = {
            "id": workflow["id"],
            "name": workflow["name"],
            "status": workflow["status"],
            "sessionId": workflow["sessionId"],
            "createdAt": workflow["createdAt"],
            "startedAt": workflow["startedAt"],
            "finishedAt": workflow["finishedAt"],
            "steps": [self._public_step(item, detail, summary) for item in steps],
        }
        if workflow["error"]:
            snapshot["error"] = workflow["error"]
        if detail and not summary and step is None:
            # The dependency closure is scheduling bookkeeping, not part of the document.
            # A summary or a step selector is a context-economy request: the spec repeats
            # every prompt, which is the payload those selectors exist to avoid.
            snapshot["spec"] = {key: value for key, value in workflow["spec"].items()
                                if key != "ancestors"}
        return snapshot

    @staticmethod
    def _public_step(step, detail, summary=False):
        view = {
            "id": step["id"],
            "agentId": step["agentId"],
            "needs": step["needs"],
            "status": "pending" if step["status"] == "ready" else step["status"],
            "needsPolicy": step["_spec"].get("needsPolicy"),
            "attempts": step["attempts"],
            "startedAt": step["startedAt"],
            "finishedAt": step["finishedAt"],
        }
        if step["error"]:
            view["error"] = step["error"]
        if summary:
            # Size and first line answer "did it finish, and what is it about" without the
            # transcript; the full output stays one step-selector call away.
            output = step["output"] or ""
            view["attemptTimes"] = [dict(window) for window in step["attemptTimes"]]
            view["outputChars"] = len(output)
            lines = output.splitlines()
            view["firstLine"] = lines[0] if lines else None
            return view
        if detail:
            view["taskIds"] = list(step["taskIds"])
            view["attemptTimes"] = [dict(window) for window in step["attemptTimes"]]
            if step["output"] is not None:
                view["output"] = step["output"]
            if step["errorDetail"]:
                view["errorDetail"] = step["errorDetail"]
        return view
