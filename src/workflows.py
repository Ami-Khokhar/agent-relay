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

Input strings are templates: ``{{stepId.output}}`` (or bare ``{{stepId}}``) resolves
against a step this one needs, directly or through another step.
"""
from __future__ import annotations

import re
import threading
import uuid

STEP_ID_RE = re.compile(r"^[A-Za-z0-9_.~-]{1,128}$")
RUN_ID_RE = re.compile(r"^wf-[0-9a-f]{32}$")
TEMPLATE_RE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")

WORKFLOW_TERMINAL = frozenset(("completed", "failed", "cancelled"))
STEP_TERMINAL = frozenset(("completed", "failed", "skipped", "cancelled"))
TASK_TERMINAL = frozenset(("completed", "failed", "timed_out", "cancelled"))

WORKFLOW_FIELDS = frozenset(("name", "sessionId", "requestId", "steps"))
STEP_FIELDS = frozenset(("id", "agentId", "input", "needs", "timeoutMs", "cwd"))


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


def validate_spec(spec, agents, max_steps, max_body, max_command_input,
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

        steps.append({
            "id": step_id,
            "agentId": agent_id,
            "input": text,
            "needs": list(needs),
            "timeoutMs": timeout,
            "cwd": cwd,
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
                 agents, resolve_cwd, max_steps, max_workflows, max_body,
                 max_command_input, max_timeout_ms, now):
        self._lock = lock
        self._condition = condition
        self._create_task = create_task
        self._get_task = get_task
        self._cancel_task = cancel_task
        self._drain = drain
        self._agents = agents
        self._resolve_cwd = resolve_cwd
        self._max_steps = max_steps
        self._max_workflows = max_workflows
        self._max_body = max_body
        self._max_command_input = max_command_input
        self._max_timeout_ms = max_timeout_ms
        self._now = now
        self._workflows = {}
        self._request_ids = {}

    # ------------------------------------------------------------------ public API

    def submit(self, spec):
        """Validate and start a workflow. Returns ``(http_status, body)``."""
        agents = self._agents()
        normalized, problem = validate_spec(
            spec, agents, self._max_steps, self._max_body,
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

        with self._lock:
            if request_id is not None:
                prior = self._request_ids.get(request_id)
                if prior is not None:
                    if prior["_requested_spec"] != normalized:
                        return 409, {"error": "idempotency_conflict"}
                    return 200, self._public(prior, detail=False)
            if not self._make_room():
                return 503, {"error": "workflow_capacity_reached"}
            workflow = self._new_workflow(normalized, request_id)
            self._workflows[workflow["id"]] = workflow
            if request_id is not None:
                self._request_ids[request_id] = workflow
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

    def public(self, workflow_id, detail=True):
        if not RUN_ID_RE.fullmatch(workflow_id or ""):
            return None
        with self._lock:
            workflow = self._workflows.get(workflow_id)
            return self._public(workflow, detail=detail) if workflow else None

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
        }

    @staticmethod
    def _new_step(step):
        return {
            "id": step["id"],
            "agentId": step["agentId"],
            "needs": step["needs"],
            "status": "pending",
            "attempts": 0,
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

    def _context(self, workflow, step):
        outputs = {other["id"]: other["output"] for other in workflow["steps"]
                   if other["status"] == "completed"}
        context = {}
        for dep in workflow["_ancestors"][step["id"]]:
            value = outputs.get(dep) or ""
            context[dep] = value
            context[f"{dep}.output"] = value
        return context
    def _launch(self, workflow, step):
        """Render the step's input and submit its task."""
        spec = step["_spec"]
        try:
            text = _render(spec["input"], self._context(workflow, step))
        except KeyError as exc:
            step["status"] = "failed"
            step["error"] = f"unresolved template reference: {exc.args[0]}"
            return True
        request = {"agentId": spec["agentId"], "input": text,
                   "sessionId": workflow["sessionId"]}
        if spec["timeoutMs"] is not None:
            request["timeoutMs"] = spec["timeoutMs"]
        if spec["cwd"] is not None:
            request["cwd"] = spec["cwd"]
        status, body = self._create_task(request)
        if status not in (200, 202):
            step["status"] = "failed"
            # Keep the relay's code: a document the agent cannot honour must be fixable
            # without guessing which rule refused it.
            code = body.get("error")
            message = body.get("message") or code or "task_rejected"
            step["error"] = f"{code}: {message}" if code and code not in message else message
            return True
        step["attempts"] += 1
        step["_current"] = [body["id"]]
        step["taskIds"].append(body["id"])
        step["status"] = "running"
        return True
    def _reap(self, workflow, step):
        """Fold the finished task back into the step's state machine."""
        task = self._get_task(step["_current"][0]) if step["_current"] else None
        if task is None:
            step["status"] = "failed"
            step["error"] = "task_evicted_from_the_relay_store"
            return True
        if task["status"] not in TASK_TERMINAL:
            return False
        if task["status"] in ("failed", "timed_out"):
            step["status"] = "failed"
            step["error"] = task.get("error") or task["status"]
            step["output"] = task.get("output") or None
            return True
        if task["status"] == "cancelled":
            step["status"] = "cancelled"
            step["error"] = "task_cancelled"
            return True
        step["output"] = task.get("output") or ""
        step["error"] = None
        step["status"] = "completed"
        return True
    def _finish(self, workflow, status):
        workflow["status"] = status
        workflow["finishedAt"] = self._now()
        self._condition.notify_all()

    # ------------------------------------------------------------------- journaling

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
