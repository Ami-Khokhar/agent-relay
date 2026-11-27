"""Bounded automatic wakes for workers with previously dispatched assignments."""
import hashlib
import time


DISCUSSION_KINDS = {"message", "question", "proposal", "challenge", "request", "advice"}
ACTIVE = {"preparing", "queued", "running", "submission_unknown"}


class TeamWake:
    """Dispatch at most one durable wake for an unread addressed discussion event.

    ``dispatch`` receives ``(run_id, assignment_id, request_id, provenance)``.
    The callback owns trusted credentials and dispatch context; provenance never
    contains credentials and is recovered from the TeamStore after restart.
    """

    def __init__(self, store, dispatch):
        self.store = store
        self.dispatch = dispatch

    def wake(self, run_id, participant_id=None):
        run = self.store.get_run(run_id)
        if run["closed"]:
            return [{"status": "skipped", "reason": "run_closed"}]
        maximum = run.get("limits", {}).get("max_elapsed_seconds")
        if maximum is not None and time.time() - run["created"] >= maximum:
            return [{"status": "skipped", "reason": "time_limit"}]
        assignments = {a["participant_id"]: a for a in run.get("assignments", [])}
        participants = {p["id"] for p in run.get("participants", []) if p["role"] != "lead"}
        if participant_id is not None:
            participants &= {participant_id}
        results = []
        attempts = self.store.list_attempts(run_id)
        by_request = {attempt["request_id"]: attempt for attempt in attempts}
        active_by_assignment = {attempt["assignment_id"]: attempt for attempt in attempts
                                if attempt["status"] in ACTIVE}
        for recipient in sorted(participants):
            assignment = assignments.get(recipient)
            if not assignment:
                continue
            revision = assignment["revision"]
            provenance = self.store.get_dispatch_provenance(run_id, assignment["id"], revision)
            if provenance is None:
                continue
            inbox = self.store.list_inbox(run_id, recipient, unread_only=True)
            for event in inbox:
                if event["kind"] not in DISCUSSION_KINDS or event["seq"] <= provenance["after_seq"]:
                    continue
                request_id = self._request_id(run_id, assignment["id"], revision, event["id"])
                existing = by_request.get(request_id)
                if existing:
                    results.append(self._result(event, assignment, "skipped", "already_processed", existing))
                    continue
                active = active_by_assignment.get(assignment["id"])
                if active:
                    results.append(self._result(event, assignment, "skipped", "assignment_active", active))
                    break
                try:
                    attempt = self.dispatch(run_id, assignment["id"], request_id, provenance)
                except Exception as exc:
                    code = getattr(exc, "code", "dispatch_failed")
                    status = "skipped" if code == "assignment_active" else "failed"
                    results.append(self._result(event, assignment, status, code, None))
                    return results
                results.append(self._result(event, assignment, "dispatched", None, attempt))
                # A single inbound message can create at most one new attempt per call.
                return results
        return results

    @staticmethod
    def _request_id(run_id, assignment_id, revision, event_id):
        raw = "\0".join((run_id, assignment_id, str(revision), event_id))
        return "wake:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _result(event, assignment, status, reason, attempt):
        return {"event_id": event["id"], "assignment_id": assignment["id"],
                "status": status, "reason": reason, "attempt": attempt}
