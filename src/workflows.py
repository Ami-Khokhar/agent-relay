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
out with ``foreach``, re-run on a task failure with ``retries``, and loop until their
output matches a regular expression with ``repeat``.

Every run is journaled to ``<state dir>/workflows/<id>.jsonl``. Completed steps therefore
survive a relay restart, and ``resume`` continues a run without repeating finished work.
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
STEP_FIELDS = frozenset(("id", "agentId", "input", "needs", "foreach", "timeoutMs", "cwd",
                         "options", "readOnly", "retries", "repeat"))
REPEAT_FIELDS = frozenset(("max", "untilOutputMatches"))

MAX_REPEAT_ROUNDS = 10
MAX_RETRIES = 5
MAX_ITEM_CHARS = 1024


def _text(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit


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
            "foreach": list(foreach) if foreach is not None else None,
            "timeoutMs": timeout,
            "cwd": cwd,
            "options": dict(options) if options is not None else None,
            "readOnly": read_only,
            "retries": retries,
            "repeat": repeat_value,
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
                 max_body,
                 max_command_input, max_timeout_ms, now):
        self._lock = lock
        self._condition = condition
        self._create_task = create_task
        self._get_task = get_task
        self._cancel_task = cancel_task
        self._drain = drain
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

    # ------------------------------------------------------------------ public API

    def submit(self, spec, refusal=None):
        """Validate and start a workflow. Returns ``(http_status, body)``.

        ``refusal`` is asked, once, whether the host can take a new run, and its non-None answer
        becomes the response. It is consulted after the idempotent lookup, so a replay of a run
        that already exists still gets that run back instead of a refusal that would lose its id.
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
        # Asked before the lock is taken, so a probe never blocks the scheduler on a slow host.
        refused = refusal() if refusal is not None else None

        with self._lock:
            if request_id is not None:
                prior = self._request_ids.get(request_id)
                if prior is not None:
                    if prior["_requested_spec"] != normalized:
                        return 409, {"error": "idempotency_conflict"}
                    return 200, self._public(prior, detail=False)
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

    def public(self, workflow_id, detail=True):
        if not RUN_ID_RE.fullmatch(workflow_id or ""):
            return None
        with self._lock:
            workflow = self._workflows.get(workflow_id)
            return self._public(workflow, detail=detail) if workflow else None

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
            workflow = self._new_workflow(saved["spec"], None, workflow_id=workflow_id)
            for step in workflow["steps"]:
                done = saved["outputs"].get(step["id"])
                if done is not None:
                    step["status"] = "completed"
                    step["output"] = done["output"]
                    step["attempts"] = done["attempts"]
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
            "taskIds": [],
            "output": None,
            "error": None,
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
                    workflow["error"] = f"step {culprit['id']} {culprit['status']}"
                    self._finish(workflow, "failed")
                else:
                    self._finish(workflow, "completed")
                return True
            self._condition.notify_all()
        if launched:
            self._drain()
        return False

    def _release(self, workflow, step, by_id):
        """Move a step from pending to ready, or skip it when a dependency failed."""
        dependencies = [by_id[dep] for dep in step["_spec"]["needs"]]
        if any(dep["status"] in ("failed", "skipped", "cancelled") for dep in dependencies):
            step["status"] = "skipped"
            step["error"] = "dependency_not_completed"
            return True
        if all(dep["status"] == "completed" for dep in dependencies):
            step["status"] = "ready"
            return True
        return False

    def _context(self, workflow, step, item, index):
        outputs = {other["id"]: other["output"] for other in workflow["steps"]
                   if other["status"] == "completed"}
        context = {}
        for dep in workflow["_ancestors"][step["id"]]:
            value = outputs.get(dep) or ""
            context[dep] = value
            context[f"{dep}.output"] = value
        if step["_spec"]["foreach"] is not None:
            context["item"] = item
            context["index"] = str(index)
            context["foreach.item"] = item
            context["foreach.index"] = str(index)
        return context

    def _launch(self, workflow, step):
        """Render and submit every task this attempt needs."""
        spec = step["_spec"]
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
                return True
            submitted.append(body["id"])
        step["attempts"] = attempt
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
                return True
            step["status"] = "failed"
            step["error"] = failures[0].get("error") or failures[0]["status"]
            step["output"] = failures[0].get("output") or None
            return True

        cancelled = [task for task in tasks if task["status"] == "cancelled"]
        if cancelled:
            step["status"] = "cancelled"
            step["error"] = "task_cancelled"
            return True

        outputs = [task.get("output") or "" for task in tasks]
        step["output"] = json.dumps(outputs) if spec["foreach"] is not None else outputs[0]
        step["error"] = None

        if spec["repeat"] is not None:
            pattern = spec["repeat"]["untilOutputMatches"]
            if re.search(pattern, step["output"]) is None:
                if step["attempts"] < spec["repeat"]["max"]:
                    step["_current"] = []
                    step["status"] = "ready"
                    return True
                step["status"] = "failed"
                step["error"] = "repeat_limit_reached"
                return True

        step["status"] = "completed"
        self._journal(workflow, {"type": "step", "at": self._now(), "step": step["id"],
                                 "status": "completed", "output": step["output"],
                                 "attempts": step["attempts"]})
        return True

    def _cancel_children(self, workflow):
        for step in workflow["steps"]:
            if step["status"] in STEP_TERMINAL:
                continue
            self._abort_children(step["_current"])
            step["status"] = "cancelled"
            step["error"] = "workflow_cancelled"
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
                }
        if not isinstance(spec, dict) or "ancestors" not in spec:
            return None
        return {"spec": spec, "outputs": outputs}

    # ------------------------------------------------------------------- presentation

    def _public(self, workflow, detail):
        if workflow is None:
            return None
        view = {
            "id": workflow["id"],
            "name": workflow["name"],
            "status": workflow["status"],
            "sessionId": workflow["sessionId"],
            "createdAt": workflow["createdAt"],
            "startedAt": workflow["startedAt"],
            "finishedAt": workflow["finishedAt"],
            "steps": [self._public_step(step, detail) for step in workflow["steps"]],
        }
        if workflow["error"]:
            view["error"] = workflow["error"]
        if detail:
            # The dependency closure is scheduling bookkeeping, not part of the document.
            view["spec"] = {key: value for key, value in workflow["spec"].items()
                            if key != "ancestors"}
        return view

    @staticmethod
    def _public_step(step, detail):
        view = {
            "id": step["id"],
            "agentId": step["agentId"],
            "needs": step["needs"],
            "status": "pending" if step["status"] == "ready" else step["status"],
            "attempts": step["attempts"],
        }
        if step["error"]:
            view["error"] = step["error"]
        if detail:
            view["taskIds"] = list(step["taskIds"])
            if step["output"] is not None:
                view["output"] = step["output"]
        return view
