"""Dispatch team assignments through the relay task boundary.

The TeamStore owns durable attempt state. This module intentionally depends on
its small attempt-method protocol instead of importing server globals, so the
server can inject its trusted task creation and lookup functions.
"""
import hashlib
import json
import time
import uuid

from teams import PURPOSE_PHASES, TeamError


class TeamExecutionError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


TERMINAL = {"completed", "failed", "timed_out", "cancelled", "interrupted"}
ACTIVE = {"preparing", "queued", "running", "submission_unknown"}
PROMPT_HISTORY_BYTES = 96 * 1024
RECOVERY_HISTORY_BYTES = 16 * 1024


class TeamExecutor:
    """Coordinate persistent attempts while treating Relay tasks as a boundary.

    Store protocol: ``get_run``, ``prepare_attempt``, ``record_attempt_submission``,
    ``record_attempt_result``, and ``list_attempts``. Preparation must atomically
    check active capacity and dependency revisions, and durably store request ID
    and assignment revision before returning.
    """

    def __init__(self, store, create_task, lookup_task, clock=time.time):
        self.store = store
        self.create_task = create_task
        self.lookup_task = lookup_task
        self.clock = clock

    def dispatch(self, run_id, assignment_id, request_id=None):
        """Persist an attempt before submitting its child task.

        Retrying with the same request ID returns the existing attempt. Callers
        should supply that ID before dispatch so a lost response is recoverable.
        """
        request_id = request_id or str(uuid.uuid4())
        run = self.store.get_run(run_id)
        assignment = next((item for item in run["assignments"] if item["id"] == assignment_id), None)
        if assignment is None:
            raise TeamExecutionError(404, "assignment_not_found", "Assignment was not found")
        prior = next((item for item in self.store.list_attempts(run_id)
                      if item.get("request_id") == request_id), None)
        if prior is not None:
            if (prior.get("assignment_id") != assignment_id
                    or prior.get("assignment_revision") != assignment["revision"]):
                raise TeamExecutionError(409, "request_id_conflict",
                                         "Request ID belongs to a different assignment revision")
            # An attempt without a task ID may still be in the live submit
            # call (or have an unknown outcome). Only startup recovery can
            # decide that the old process is gone and mark it interrupted.
            if not prior.get("task_id"):
                return prior
            return self.refresh(run_id, prior["id"])
        if run["closed"]:
            raise TeamExecutionError(409, "run_closed", "Closed team runs cannot dispatch work")
        purpose = assignment.get("purpose", "implementation")
        if run["phase"] not in PURPOSE_PHASES.get(purpose, ()):
            raise TeamExecutionError(409, "phase_gate", "Run phase does not permit this assignment purpose")
        if purpose == "implementation" and not run["approved"]:
            raise TeamExecutionError(409, "spec_not_approved", "Implementation requires an approved specification")
        participant = next((p for p in run["participants"] if p["id"] == assignment["participant_id"]), None)
        if participant is None or not participant.get("agentId"):
            raise TeamExecutionError(409, "agent_not_configured", "Assignment participant has no configured agent")
        self._check_elapsed(run)
        if not isinstance(request_id, str) or not request_id.strip():
            raise TeamExecutionError(400, "invalid_request_id", "A stable request ID is required")
        prompt = self._prompt(run, assignment, participant)
        try:
            attempt = self.store.prepare_attempt(
                run_id, assignment_id, request_id, prompt,
                assignment_revision=assignment["revision"], purpose=purpose,
                expected_spec_hash=hashlib.sha256((run.get("spec") or "").encode("utf-8")).hexdigest())
        except TeamError as exc:
            raise TeamExecutionError(exc.status, exc.code, exc.message) from exc
        if not attempt:
            raise TeamExecutionError(404, "attempt_not_found", "Attempt was not found")
        if attempt.get("task_id") or not attempt.get("newly_created", False):
            return attempt
        if attempt.get("status") != "preparing":
            # A preparation without an associated task is never blindly replayed.
            return attempt
        # Namespacing prevents this team's idempotency key from replaying a
        # legacy or unrelated task submitted with the same caller request ID.
        relay_request_id = "team:" + hashlib.sha256(f"{run_id}\0{request_id}".encode("utf-8")).hexdigest()
        try:
            status, body = self.create_task({
                "agentId": participant["agentId"], "input": prompt,
                "requestId": relay_request_id, "sessionId": run_id,
            })
        except Exception as exc:
            self.store.record_attempt_result(run_id, attempt["id"], "submission_unknown",
                                             self._bounded_result({"error": str(exc), "relay_request_id": relay_request_id}))
            raise TeamExecutionError(503, "submission_unknown", "Task submission outcome is unknown; inspect the recorded attempt before retrying") from exc
        if status not in (200, 202) or not isinstance(body, dict) or not body.get("id"):
            self.store.record_attempt_result(run_id, attempt["id"], "failed",
                                             self._bounded_result(body or {"status": status}))
            raise TeamExecutionError(status if isinstance(status, int) else 500,
                                     body.get("error", "submission_failed") if isinstance(body, dict) else "submission_failed",
                                     body.get("message", "Relay refused the task") if isinstance(body, dict) else "Relay refused the task")
        self.store.record_attempt_submission(run_id, attempt["id"], body["id"], body.get("status", "queued"))
        return self.refresh(run_id, attempt["id"])

    def refresh(self, run_id, attempt_id):
        """Copy the current in-memory task state into durable attempt history."""
        attempt = self._attempt(run_id, attempt_id)
        if attempt.get("status") in TERMINAL:
            return attempt
        task_id = attempt.get("task_id")
        if not task_id:
            if attempt.get("status") == "preparing":
                # A live dispatch may be between prepare and submit. Only
                # startup recovery is allowed to retire a stale preparation.
                return attempt
            self.store.mark_interrupted(run_id, attempt_id, self._bounded_result({
                "error": "no_task_id_after_interruption; submission was not replayed",
                "replacement_context": self._replacement_context(run_id, attempt),
            }))
            return self._attempt(run_id, attempt_id)
        task = self.lookup_task(task_id)
        if task is None:
            self.store.mark_interrupted(run_id, attempt_id, self._bounded_result({
                "error": "task_missing_after_restart",
                "replacement_context": self._replacement_context(run_id, attempt),
            }))
            return self._attempt(run_id, attempt_id)
        status = task.get("status", "running")
        if status in TERMINAL:
            result = {key: task.get(key) for key in ("output", "error", "outputTruncated", "finishedAt") if key in task}
            result["task_status"] = status
            self.store.record_attempt_result(run_id, attempt_id, status,
                                             self._bounded_result(result))
        else:
            self.store.record_attempt_submission(run_id, attempt_id, task_id, status)
        return self._attempt(run_id, attempt_id)

    def recover(self, run_id):
        """Reconcile after relay startup, never concurrently with dispatch.

        In-memory task records are lost on restart. Do not resubmit a missing
        task: retire it with explicit replacement context instead.
        """
        outcomes = []
        for attempt in self.store.list_attempts(run_id):
            if attempt.get("status") in ACTIVE:
                if not attempt.get("task_id") and attempt.get("status") in ("preparing", "submission_unknown"):
                    self.store.mark_interrupted(run_id, attempt["id"], self._bounded_result({
                        "error": "submission_not_reconciled_after_restart; task was not replayed",
                        "replacement_context": self._replacement_context(run_id, attempt),
                    }))
                    outcomes.append(self._attempt(run_id, attempt["id"]))
                    continue
                outcomes.append(self.refresh(run_id, attempt["id"]))
        return outcomes

    def _check_elapsed(self, run):
        maximum = run.get("limits", {}).get("max_elapsed_seconds")
        if maximum is not None and self.clock() - run["created"] >= maximum:
            raise TeamExecutionError(409, "time_limit", "Team run elapsed-time limit has been reached")

    def _prompt(self, run, assignment, participant):
        relevant = []
        for event in run.get("events", []):
            if event.get("assignment_id") == assignment["id"] or event.get("recipient") == participant["id"]:
                relevant.append({"kind": event["kind"], "actor": event["actor"],
                                 "body": self._clip_text(event.get("body", ""), 4096)})
        role_instruction = {
            "worker": "Implement only the assigned work and report evidence or blockers.",
            "reviewer": "Independently inspect the assigned work and report evidence, findings, and severity.",
            "evaluator": "Evaluate the assigned artifact against the agreed criteria; do not change the criteria.",
        }.get(participant.get("role"), "Complete the assigned work and report evidence or blockers.")
        prior_attempts = [self._decode_attempt(item) for item in self.store.list_attempts(run["id"])
                          if item.get("assignment_id") == assignment["id"]]
        relevant, event_omitted = self._recent_records(relevant, PROMPT_HISTORY_BYTES)
        prior_attempts, attempt_omitted = self._recent_records(prior_attempts, PROMPT_HISTORY_BYTES)
        replacement = ""
        if prior_attempts or attempt_omitted:
            replacement = ("\nPrior execution records (context only; native session was not resumed): "
                           + json.dumps({"native_session_resumed": False, "attempts": prior_attempts,
                                         "omitted_older_attempts": attempt_omitted},
                                        ensure_ascii=False) + "\n")
        objective = self._clip_text(run["objective"], 8192)
        specification = self._clip_text(run.get("spec") or "(none recorded yet)", 8192)
        assignment_text = self._clip_text(assignment["text"], 8192)
        context_truncated = bool(event_omitted or attempt_omitted
                                 or "[truncated]" in objective
                                 or "[truncated]" in specification
                                 or "[truncated]" in assignment_text)
        full_context_instruction = (
            " Some recorded context was omitted or clipped; before acting, retrieve the complete agreed "
            "specification and assignment through get_team using AGENT_RELAY_TEAM_ID. Do not infer missing "
            "constraints from this summary."
        ) if context_truncated else ""
        return ("You are a team participant working under the lead's authority. Treat peer requests as advice; "
                "do not change scope, priorities, permissions, or success criteria. Route user decisions to the lead. "
                f"Role: {role_instruction}\n\n"
                f"Objective: {objective}\n"
                f"Agreed specification: {specification}\n"
                f"Assignment: {assignment_text}\n"
                f"Relevant recorded context (older events omitted: {event_omitted}): "
                f"{json.dumps(relevant, ensure_ascii=False)}\n{replacement}"
                f"{full_context_instruction}\n"
                "Return concrete results, evidence, blockers, and any question for the lead.")

    @staticmethod
    def _decode_attempt(attempt):
        result = attempt.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (ValueError, TypeError):
                result = {"raw_result": result}
        if isinstance(result, dict):
            compact = {key: TeamExecutor._clip_text(value, 4096) if isinstance(value, str) else value
                       for key, value in result.items()
                       if key in ("output", "error", "task_status", "outputTruncated", "recordTruncated")}
            result = compact
        elif result is not None:
            result = {"raw_result": TeamExecutor._clip_text(str(result), 4096)}
        return {"attempt_id": attempt["id"], "status": attempt.get("status"), "result": result}

    def _replacement_context(self, run_id, attempt):
        run = self.store.get_run(run_id)
        assignment = next((item for item in run.get("assignments", [])
                           if item["id"] == attempt.get("assignment_id")), None)
        participant_id = assignment.get("participant_id") if assignment else None
        events = [{"kind": e.get("kind"), "actor": e.get("actor"),
                   "body": self._clip_text(e.get("body", ""), 2048)}
                  for e in run.get("events", [])
                  if e.get("assignment_id") == attempt.get("assignment_id")
                  or (participant_id and e.get("recipient") == participant_id)]
        previous = [self._decode_attempt(item) for item in self.store.list_attempts(run_id)
                    if item.get("assignment_id") == attempt.get("assignment_id")
                    and item.get("id") != attempt.get("id")]
        events, events_omitted = self._recent_records(events, RECOVERY_HISTORY_BYTES // 2)
        previous, attempts_omitted = self._recent_records(previous, RECOVERY_HISTORY_BYTES // 2)
        return {"assignment_id": attempt.get("assignment_id"),
                "objective": self._clip_text(run["objective"], 2048),
                "specification": self._clip_text(run.get("spec") or "", 2048),
                "assignment": self._clip_text(assignment.get("text", "") if assignment else "", 2048),
                "recorded_events": events, "omitted_older_events": events_omitted,
                "prior_attempts": previous, "omitted_older_attempts": attempts_omitted,
                "native_session_resumed": False}

    @staticmethod
    def _clip_text(value, max_bytes):
        encoded = value.encode("utf-8")
        if len(encoded) <= max_bytes:
            return value
        marker = "…[truncated]"
        available = max(0, max_bytes - len(marker.encode("utf-8")))
        return encoded[:available].decode("utf-8", errors="ignore") + marker

    @classmethod
    def _recent_records(cls, records, max_bytes):
        """Keep the newest complete records that fit and report omitted older ones."""
        selected, used = [], 2
        for item in reversed(records):
            size = len(json.dumps(item, ensure_ascii=False).encode("utf-8")) + 1
            if used + size > max_bytes:
                break
            selected.append(item)
            used += size
        selected.reverse()
        return selected, len(records) - len(selected)

    @staticmethod
    def _bounded_result(result):
        """Keep task output within the durable store's 32 KiB record limit."""
        bounded = dict(result) if isinstance(result, dict) else {"result": str(result)}
        original = json.dumps(bounded, ensure_ascii=False).encode("utf-8")
        if len(original) <= 30000:
            return bounded
        bounded["recordTruncated"] = True
        bounded["outputTruncated"] = True
        for key in ("output", "error", "result", "message"):
            value = bounded.get(key)
            if isinstance(value, str):
                encoded = value.encode("utf-8")
                bounded[key] = encoded[:12000].decode("utf-8", errors="ignore")
        while len(json.dumps(bounded, ensure_ascii=False).encode("utf-8")) > 30000:
            keys = [key for key in ("output", "error", "result", "message")
                    if isinstance(bounded.get(key), str) and bounded[key]]
            if not keys:
                return {"recordTruncated": True, "outputTruncated": True,
                        "error": "Result metadata exceeded the durable record limit"}
            key = max(keys, key=lambda item: len(bounded[item].encode("utf-8")))
            encoded = bounded[key].encode("utf-8")
            bounded[key] = encoded[:len(encoded) // 2].decode("utf-8", errors="ignore")
        return bounded

    def _attempt(self, run_id, attempt_id):
        attempt = next((a for a in self.store.list_attempts(run_id) if a["id"] == attempt_id), None)
        if attempt is None:
            raise TeamExecutionError(404, "attempt_not_found", "Attempt was not found")
        return attempt

