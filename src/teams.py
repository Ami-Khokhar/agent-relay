"""Durable, local coordination records for Relay team runs."""
import json
import hashlib
import math
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager


class TeamError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _id():
    return uuid.uuid4().hex


def _now():
    return time.time()


# One source for the purpose-to-phase gate: the execution boundary imports
# this map instead of repeating it.
PURPOSE_PHASES = {"research": ("exploration", "specification", "review"),
                  "prototype": ("prototype",), "implementation": ("build",),
                  "review": ("build", "review")}


class TeamStore:
    PHASES = {"exploration", "prototype", "specification", "build", "review", "complete"}
    DEFAULT_LIMITS = {"max_messages": 100, "max_concurrency": 4, "max_elapsed_seconds": 3600}

    def __init__(self, path):
        self.path = str(path)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, objective TEXT NOT NULL,
                    lead TEXT NOT NULL, phase TEXT NOT NULL, spec TEXT, approved INTEGER NOT NULL,
                    closed INTEGER NOT NULL, limits TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS participants(run_id TEXT NOT NULL, id TEXT NOT NULL,
                    role TEXT NOT NULL, harness TEXT, model TEXT, agent_id TEXT, PRIMARY KEY(run_id,id));
                CREATE TABLE IF NOT EXISTS assignments(id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    participant_id TEXT NOT NULL, text TEXT NOT NULL, dependencies TEXT NOT NULL,
                    updated REAL NOT NULL, purpose TEXT NOT NULL DEFAULT 'implementation',
                    revision INTEGER NOT NULL DEFAULT 1, dependency_revisions TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    run_id TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL, recipient TEXT,
                    thread_id TEXT, assignment_id TEXT, body TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    assignment_id TEXT NOT NULL, assignment_revision INTEGER NOT NULL,
                    request_id TEXT NOT NULL, prompt TEXT NOT NULL, purpose TEXT NOT NULL,
                    status TEXT NOT NULL, task_id TEXT, result TEXT, created REAL NOT NULL,
                    updated REAL NOT NULL, UNIQUE(run_id,request_id));
                CREATE TABLE IF NOT EXISTS deliveries(event_id TEXT NOT NULL, recipient TEXT NOT NULL,
                    acknowledged REAL, PRIMARY KEY(event_id,recipient));
                CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    actor TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, uri TEXT NOT NULL,
                    created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS user_questions(event_id TEXT PRIMARY KEY, answer_event_id TEXT);
                CREATE TABLE IF NOT EXISTS dispatch_provenance(run_id TEXT NOT NULL, assignment_id TEXT NOT NULL,
                    assignment_revision INTEGER NOT NULL, source_repo TEXT, base_commit TEXT,
                    branch TEXT, worktree TEXT,
                    after_seq INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
                    PRIMARY KEY(run_id,assignment_id,assignment_revision));
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(assignments)")}
            if "purpose" not in columns:
                db.execute("ALTER TABLE assignments ADD COLUMN purpose TEXT NOT NULL DEFAULT 'implementation'")
            if "revision" not in columns:
                db.execute("ALTER TABLE assignments ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
            if "dependency_revisions" not in columns:
                db.execute("ALTER TABLE assignments ADD COLUMN dependency_revisions TEXT NOT NULL DEFAULT '{}'")
            pcolumns = {r[1] for r in db.execute("PRAGMA table_info(participants)")}
            if "agent_id" not in pcolumns:
                db.execute("ALTER TABLE participants ADD COLUMN agent_id TEXT")
            dcolumns = {r[1] for r in db.execute("PRAGMA table_info(dispatch_provenance)")}
            if "after_seq" not in dcolumns:
                db.execute("ALTER TABLE dispatch_provenance ADD COLUMN after_seq INTEGER NOT NULL DEFAULT 0")
            if "branch" not in dcolumns:
                db.execute("ALTER TABLE dispatch_provenance ADD COLUMN branch TEXT")
            if "worktree" not in dcolumns:
                db.execute("ALTER TABLE dispatch_provenance ADD COLUMN worktree TEXT")
            db.execute("PRAGMA user_version=6")
            db.execute("INSERT OR IGNORE INTO deliveries(event_id,recipient) SELECT id,recipient FROM events WHERE recipient IS NOT NULL")
            for row in db.execute("SELECT id,limits FROM runs"):
                limits = json.loads(row["limits"])
                merged = dict(self.DEFAULT_LIMITS, **limits)
                if merged != limits:
                    db.execute("UPDATE runs SET limits=? WHERE id=?", (json.dumps(merged), row["id"]))

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _run(self, db, run_id):
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise TeamError(404, "run_not_found", "Team run was not found")
        return row

    def _actor(self, db, run, actor):
        if not isinstance(actor, str) or not actor:
            raise TeamError(403, "not_participant", "Actor must be a participant identity")
        row = db.execute("SELECT role FROM participants WHERE run_id=? AND id=?", (run["id"], actor)).fetchone()
        if not row:
            raise TeamError(403, "not_participant", "Actor is not a participant in this run")
        return row["role"]

    def _writable(self, run):
        if run["closed"]:
            raise TeamError(409, "run_closed", "Closed team runs cannot be changed")
        limits = json.loads(run["limits"])
        elapsed = _now() - run["created"]
        if limits.get("max_elapsed_seconds") is not None and elapsed >= limits["max_elapsed_seconds"]:
            raise TeamError(409, "time_limit", "Team run elapsed-time limit has been reached")

    def _check_messages(self, db, run):
        limit = json.loads(run["limits"]).get("max_messages")
        count = db.execute("SELECT count(*) FROM events WHERE run_id=? AND kind IN ('message','question','proposal','challenge','request','advice')", (run["id"],)).fetchone()[0]
        if limit is not None and count >= limit:
            raise TeamError(409, "message_limit", "Team run message limit has been reached")

    def _event(self, db, run_id, kind, actor, body, recipient=None, thread_id=None, assignment_id=None):
        event_id, created = _id(), _now()
        db.execute("INSERT INTO events(id,run_id,kind,actor,recipient,thread_id,assignment_id,body,created) VALUES(?,?,?,?,?,?,?,?,?)",
                   (event_id, run_id, kind, actor, recipient, thread_id, assignment_id, body, created))
        if recipient is not None:
            db.execute("INSERT INTO deliveries(event_id,recipient) VALUES(?,?)", (event_id, recipient))
        db.execute("UPDATE runs SET updated=? WHERE id=?", (created, run_id))
        return event_id

    def create_run(self, objective, lead, participants, limits=None):
        if not isinstance(objective, str) or not objective.strip() or not isinstance(lead, str) or not lead.strip():
            raise TeamError(400, "invalid_run", "Objective and lead are required")
        if participants is not None and not isinstance(participants, (list, tuple)):
            raise TeamError(400, "invalid_roster", "Participants must be a list")
        roster = list(participants or [])
        if any(not isinstance(p, dict) for p in roster):
            raise TeamError(400, "invalid_roster", "Each participant must be an object")
        ids = [p.get("id") for p in roster]
        if any(not isinstance(i, str) or not i.strip() for i in ids) or lead in ids or len(set(ids)) != len(ids):
            raise TeamError(400, "invalid_roster", "Participants need unique IDs distinct from the lead")
        for p in roster:
            if any(k in p and p[k] is not None and not isinstance(p[k], str) for k in ("role", "harness", "model", "agentId")):
                raise TeamError(400, "invalid_roster", "Participant role, harness, model, and agentId must be strings")
        if limits is None:
            limits = {}
        if not isinstance(limits, dict) or any(k not in ("max_messages", "max_elapsed_seconds", "max_concurrency") for k in limits):
            raise TeamError(400, "invalid_limits", "Limits may include max_messages, max_elapsed_seconds, and max_concurrency")
        if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in limits.values()):
            raise TeamError(400, "invalid_limits", "Configured limits must be positive numbers")
        if any(k in limits and not isinstance(limits[k], int) for k in ("max_messages", "max_concurrency")):
            raise TeamError(400, "invalid_limits", "Message and concurrency limits must be integers")
        limits = dict(self.DEFAULT_LIMITS, **limits)
        run_id, now = _id(), _now()
        with self._db() as db:
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)", (run_id, objective.strip(), lead, "exploration", None, 0, 0, json.dumps(limits), now, now))
            db.execute("INSERT INTO participants VALUES(?,?,?,?,?,?)", (run_id, lead, "lead", None, None, None))
            for p in roster:
                role = p.get("role", "worker")
                if not isinstance(role, str) or not role.strip() or role == "lead":
                    raise TeamError(400, "invalid_role", "Participant role must be a nonempty, non-lead role")
                db.execute("INSERT INTO participants VALUES(?,?,?,?,?,?)", (run_id, p["id"], role, p.get("harness"), p.get("model"), p.get("agentId")))
            self._event(db, run_id, "created", lead, objective.strip())
        return self.get_run(run_id)

    def list_runs(self):
        with self._db() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM runs ORDER BY created,id")]
        return [self.get_run(i) for i in ids]

    def get_run(self, run_id):
        self._require_ids(run_id=run_id)
        with self._db() as db:
            r = dict(self._run(db, run_id))
            r["limits"] = json.loads(r["limits"])
            r["approved"] = bool(r["approved"]); r["closed"] = bool(r["closed"])
            r["participants"] = [dict(p) for p in db.execute("SELECT * FROM participants WHERE run_id=? ORDER BY id", (run_id,))]
            for participant in r["participants"]:
                participant["agentId"] = participant.pop("agent_id")
            r["assignments"] = [dict(a, dependencies=json.loads(a["dependencies"]), dependency_revisions=json.loads(a["dependency_revisions"])) for a in db.execute("SELECT * FROM assignments WHERE run_id=? ORDER BY updated,id", (run_id,))]
            r["events"] = [dict(e) for e in db.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,))]
            r["artifacts"] = [dict(a) for a in db.execute("SELECT * FROM artifacts WHERE run_id=? ORDER BY created,id", (run_id,))]
            r["provenance"] = [dict(p) for p in db.execute(
                "SELECT * FROM dispatch_provenance WHERE run_id=? ORDER BY created,assignment_id,assignment_revision",
                (run_id,))]
            return r

    def assign(self, run_id, actor, participant_id, assignment, dependencies=None, purpose="implementation"):
        self._require_ids(run_id=run_id, actor=actor, participant_id=participant_id)
        if not isinstance(assignment, str) or not assignment.strip():
            raise TeamError(400, "invalid_assignment", "Assignment text is required")
        if purpose not in ("research", "prototype", "implementation", "review"):
            raise TeamError(400, "invalid_purpose", "Purpose must be research, prototype, implementation, or review")
        if dependencies is not None and (not isinstance(dependencies, list) or any(not isinstance(d, str) for d in dependencies)):
            raise TeamError(400, "invalid_dependencies", "Dependencies must be a list of assignment IDs")
        with self._db() as db:
            run = self._run(db, run_id); self._writable(run)
            self._actor(db, run, actor)
            if actor != run["lead"]:
                raise TeamError(403, "lead_required", "Only the stored lead may change assignments")
            if not db.execute("SELECT 1 FROM participants WHERE run_id=? AND id=?", (run_id, participant_id)).fetchone() or participant_id == run["lead"]:
                raise TeamError(400, "invalid_assignee", "Assignee must be a worker in this run")
            row = db.execute("SELECT id,revision FROM assignments WHERE run_id=? AND participant_id=?", (run_id, participant_id)).fetchone()
            aid, revision, now = (row[0], row[1] + 1, _now()) if row else (_id(), 1, _now())
            if aid in (dependencies or []):
                raise TeamError(400, "invalid_dependencies", "An assignment cannot depend on itself")
            all_ids = {row[0] for row in db.execute("SELECT id FROM assignments WHERE run_id=?", (run_id,))}
            if any(dep not in all_ids for dep in (dependencies or [])):
                raise TeamError(400, "invalid_dependencies", "Dependencies must refer to assignments in this run")
            graph = {row[0]: set(json.loads(row[1])) for row in db.execute("SELECT id,dependencies FROM assignments WHERE run_id=?", (run_id,))}
            graph[aid] = set(dependencies or [])
            def reaches(node, target, seen):
                if node == target:
                    return True
                if node in seen:
                    return False
                seen.add(node)
                return any(reaches(dep, target, seen) for dep in graph.get(node, ()))
            if any(reaches(dep, aid, set()) for dep in graph[aid]):
                raise TeamError(400, "invalid_dependencies", "Assignment dependencies cannot contain a cycle")
            dependency_revisions = {dep: db.execute("SELECT revision FROM assignments WHERE id=?", (dep,)).fetchone()[0] for dep in (dependencies or [])}
            db.execute("INSERT OR REPLACE INTO assignments(id,run_id,participant_id,text,dependencies,updated,purpose,revision,dependency_revisions) VALUES(?,?,?,?,?,?,?,?,?)", (aid, run_id, participant_id, assignment.strip(), json.dumps(dependencies or []), now, purpose, revision, json.dumps(dependency_revisions)))
            self._event(db, run_id, "assignment", actor, json.dumps({"text": assignment.strip(), "purpose": purpose, "revision": revision, "dependencies": dependencies or []}), recipient=participant_id, assignment_id=aid)
            return aid

    def _attempt(self, db, run_id, attempt_id):
        row = db.execute("SELECT * FROM attempts WHERE run_id=? AND id=?", (run_id, attempt_id)).fetchone()
        if not row:
            raise TeamError(404, "attempt_not_found", "Execution attempt was not found")
        return dict(row)

    @staticmethod
    def _require_ids(**ids):
        if any(not isinstance(value, str) or not value.strip() for value in ids.values()):
            raise TeamError(400, "invalid_id", "Run, actor, participant, assignment, and thread IDs must be nonempty strings")

    @staticmethod
    def _attempt_event(db, run_id, attempt, status):
        body = json.dumps({"attemptId": attempt["id"], "assignmentId": attempt["assignment_id"],
                           "requestId": attempt["request_id"], "status": status,
                           "taskId": attempt.get("task_id")})
        db.execute("INSERT INTO events(id,run_id,kind,actor,body,created) VALUES(?,?,?,?,?,?)",
                   (_id(), run_id, "attempt", "relay", body, _now()))
        db.execute("UPDATE runs SET updated=? WHERE id=?", (_now(), run_id))

    def prepare_attempt(self, run_id, assignment_id, request_id, prompt, assignment_revision,
                        purpose="implementation", expected_spec_hash=None):
        """Persist a dispatch intent before any external task submission."""
        if not isinstance(request_id, str) or not request_id.strip() or not isinstance(prompt, str) or not prompt.strip():
            raise TeamError(400, "invalid_attempt", "Request ID and prompt are required")
        if not isinstance(assignment_revision, int) or isinstance(assignment_revision, bool) or assignment_revision < 1:
            raise TeamError(400, "invalid_attempt", "Assignment revision must be a positive integer")
        if purpose not in ("research", "prototype", "implementation", "review"):
            raise TeamError(400, "invalid_purpose", "Unsupported execution purpose")
        if expected_spec_hash is not None and (not isinstance(expected_spec_hash, str) or len(expected_spec_hash) != 64 or any(c not in "0123456789abcdef" for c in expected_spec_hash)):
            raise TeamError(400, "invalid_spec_hash", "Expected specification hash must be a lowercase SHA-256 hex digest")
        self._require_ids(run_id=run_id, assignment_id=assignment_id)
        with self._db() as db:
            run = self._run(db, run_id)
            existing = db.execute("SELECT * FROM attempts WHERE run_id=? AND request_id=?", (run_id, request_id)).fetchone()
            if existing:
                old = dict(existing)
                if (old["assignment_id"], old["assignment_revision"], old["prompt"], old["purpose"]) != (assignment_id, assignment_revision, prompt, purpose):
                    raise TeamError(409, "request_id_conflict", "Request ID was already used for a different attempt")
                old["newly_created"] = False
                return self._present_attempt(old)
            self._writable(run)
            actual_spec_hash = hashlib.sha256((run["spec"] or "").encode("utf-8")).hexdigest()
            if expected_spec_hash is not None and actual_spec_hash != expected_spec_hash:
                raise TeamError(409, "stale_spec", "Specification changed while preparing the execution attempt")
            assignment = db.execute("SELECT * FROM assignments WHERE run_id=? AND id=?", (run_id, assignment_id)).fetchone()
            if not assignment:
                raise TeamError(404, "assignment_not_found", "Assignment was not found in this run")
            if assignment["revision"] != assignment_revision:
                raise TeamError(409, "stale_assignment", "Assignment changed; prepare a new attempt with its current revision")
            if assignment["purpose"] != purpose:
                raise TeamError(409, "purpose_mismatch", "Attempt purpose must match the lead-assigned purpose")
            required_phase = PURPOSE_PHASES[purpose]
            if run["phase"] not in required_phase:
                raise TeamError(409, "phase_gate", "Run phase does not permit this assignment purpose")
            if purpose == "implementation" and not run["approved"]:
                raise TeamError(409, "spec_not_approved", "Implementation requires an approved specification")
            dependencies = json.loads(assignment["dependencies"])
            dependency_revisions = json.loads(assignment["dependency_revisions"])
            for dep in dependencies:
                prior = db.execute("SELECT a.revision, t.assignment_revision, t.status FROM assignments a LEFT JOIN attempts t ON t.assignment_id=a.id AND t.assignment_revision=a.revision WHERE a.run_id=? AND a.id=? ORDER BY t.updated DESC LIMIT 1", (run_id, dep)).fetchone()
                if not prior or prior["revision"] != dependency_revisions.get(dep) or prior["status"] != "completed":
                    raise TeamError(409, "dependency_not_ready", "Assignment dependencies must complete before dispatch")
            active_assignment = db.execute("SELECT 1 FROM attempts WHERE run_id=? AND assignment_id=? AND status IN ('preparing','queued','running','submission_unknown') LIMIT 1", (run_id, assignment_id)).fetchone()
            if active_assignment:
                raise TeamError(409, "assignment_active", "This assignment already has an active execution")
            cap = json.loads(run["limits"]).get("max_concurrency")
            if cap is not None:
                active = db.execute("SELECT count(*) FROM attempts WHERE run_id=? AND status IN ('preparing','queued','running','submission_unknown')", (run_id,)).fetchone()[0]
                if active >= cap:
                    raise TeamError(409, "concurrency_limit", "Team run active attempt limit has been reached")
            now, attempt_id = _now(), _id()
            db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (attempt_id, run_id, assignment_id, assignment_revision, request_id, prompt,
                        purpose, "preparing", None, None, now, now))
            attempt = dict(db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone())
            self._attempt_event(db, run_id, attempt, "preparing")
            attempt["newly_created"] = True
            return self._present_attempt(attempt)

    def record_attempt_submission(self, run_id, attempt_id, task_id, status):
        if status not in ("queued", "running", "submission_unknown") or (task_id is not None and (not isinstance(task_id, str) or not task_id.strip())):
            raise TeamError(400, "invalid_submission", "Submission status must be queued, running, or submission_unknown")
        self._require_ids(run_id=run_id, attempt_id=attempt_id)
        with self._db() as db:
            attempt = self._attempt(db, run_id, attempt_id)
            if attempt["status"] in ("completed", "failed", "cancelled", "interrupted", "timed_out"):
                raise TeamError(409, "attempt_terminal", "Terminal attempts cannot be resubmitted")
            if attempt["status"] in ("queued", "running", "submission_unknown"):
                if attempt["task_id"] == task_id and attempt["status"] == status:
                    return self._present_attempt(attempt)
                allowed = ((attempt["status"] == "queued" and status == "running") or
                           (attempt["status"] == "submission_unknown" and status in ("queued", "running", "submission_unknown")))
                if not allowed or (attempt["task_id"] is not None and attempt["task_id"] != task_id):
                    raise TeamError(409, "attempt_submitted", "Attempt submission was already recorded")
            db.execute("UPDATE attempts SET task_id=?,status=?,updated=? WHERE id=?", (task_id, status, _now(), attempt_id))
            attempt.update(task_id=task_id, status=status)
            self._attempt_event(db, run_id, attempt, status)
            return self._present_attempt(attempt)

    def record_attempt_result(self, run_id, attempt_id, status, result):
        if status not in ("completed", "failed", "cancelled", "interrupted", "timed_out", "submission_unknown"):
            raise TeamError(400, "invalid_result", "Unsupported attempt result status")
        self._require_ids(run_id=run_id, attempt_id=attempt_id)
        serialized = json.dumps(result, ensure_ascii=False)
        if len(serialized.encode("utf-8")) > 32768:
            raise TeamError(413, "result_too_large", "Attempt result exceeds 32 KiB")
        with self._db() as db:
            attempt = self._attempt(db, run_id, attempt_id)
            if attempt["status"] in ("completed", "failed", "cancelled", "interrupted", "timed_out"):
                if attempt["status"] == status and attempt["result"] == serialized:
                    return self._present_attempt(attempt)
                raise TeamError(409, "attempt_terminal", "Attempt results are immutable after completion")
            db.execute("UPDATE attempts SET status=?,result=?,updated=? WHERE id=?", (status, serialized, _now(), attempt_id))
            attempt.update(status=status, result=serialized)
            self._attempt_event(db, run_id, attempt, status)
            return self._present_attempt(attempt)

    def mark_interrupted(self, run_id, attempt_id, reason):
        if not ((isinstance(reason, str) and reason.strip()) or isinstance(reason, dict)):
            raise TeamError(400, "invalid_interruption", "Interruption reason is required")
        result = reason if isinstance(reason, dict) else {"reason": reason.strip()}
        return self.record_attempt_result(run_id, attempt_id, "interrupted", result)

    def list_attempts(self, run_id):
        self._require_ids(run_id=run_id)
        with self._db() as db:
            self._run(db, run_id)
            return [self._present_attempt(dict(row)) for row in db.execute("SELECT * FROM attempts WHERE run_id=? ORDER BY created,id", (run_id,))]

    def record_dispatch_provenance(self, run_id, assignment_id, assignment_revision,
                                   source_repo=None, base_commit=None, after_seq=0,
                                   branch=None, worktree=None):
        self._require_ids(run_id=run_id, assignment_id=assignment_id)
        if not isinstance(assignment_revision, int) or isinstance(assignment_revision, bool) or assignment_revision < 1:
            raise TeamError(400, "invalid_provenance", "Assignment revision must be a positive integer")
        if not isinstance(after_seq, int) or isinstance(after_seq, bool) or after_seq < 0:
            raise TeamError(400, "invalid_provenance", "Event sequence watermark must be a nonnegative integer")
        bad_repo = source_repo is not None and (not isinstance(source_repo, str) or not os.path.isabs(source_repo))
        bad_commit = base_commit is not None and (
            not isinstance(base_commit, str) or len(base_commit) not in (40, 64)
            or any(c not in "0123456789abcdefABCDEF" for c in base_commit))
        if (source_repo is None) != (base_commit is None) or bad_repo or bad_commit:
            raise TeamError(400, "invalid_provenance", "Repository path and full base commit must be provided together")
        if any(value is not None and (not isinstance(value, str) or not value.strip())
               for value in (branch, worktree)):
            raise TeamError(400, "invalid_provenance", "Branch and worktree must be nonempty strings")
        if (branch is None) != (worktree is None):
            raise TeamError(400, "invalid_provenance", "Branch and worktree must be provided together")
        with self._db() as db:
            self._run(db, run_id)
            assignment = db.execute("SELECT revision FROM assignments WHERE run_id=? AND id=?", (run_id, assignment_id)).fetchone()
            if not assignment:
                raise TeamError(404, "assignment_not_found", "Assignment was not found")
            if assignment["revision"] != assignment_revision:
                raise TeamError(409, "stale_assignment", "Assignment changed before dispatch provenance was saved")
            dispatched = db.execute("SELECT created FROM attempts WHERE run_id=? AND assignment_id=? AND assignment_revision=? AND task_id IS NOT NULL ORDER BY created DESC LIMIT 1", (run_id, assignment_id, assignment_revision)).fetchone()
            if not dispatched:
                raise TeamError(409, "attempt_not_dispatched", "Dispatch provenance requires a recorded Relay task")
            prior = db.execute("SELECT * FROM dispatch_provenance WHERE run_id=? AND assignment_id=? AND assignment_revision=?", (run_id, assignment_id, assignment_revision)).fetchone()
            if prior:
                if (prior["source_repo"], prior["base_commit"], prior["after_seq"]) != (source_repo, base_commit, after_seq):
                    raise TeamError(409, "provenance_conflict", "Dispatch context for this assignment revision is immutable")
                stored_branch = prior["branch"] or branch
                stored_worktree = prior["worktree"] or worktree
                if ((prior["branch"] is not None and prior["branch"] != branch)
                        or (prior["worktree"] is not None and prior["worktree"] != worktree)):
                    raise TeamError(409, "provenance_conflict", "Dispatch context for this assignment revision is immutable")
                if (stored_branch, stored_worktree) != (prior["branch"], prior["worktree"]):
                    db.execute("UPDATE dispatch_provenance SET branch=?,worktree=? WHERE run_id=? AND assignment_id=? AND assignment_revision=?",
                               (stored_branch, stored_worktree, run_id, assignment_id, assignment_revision))
                return dict(run_id=run_id, assignment_id=assignment_id, assignment_revision=assignment_revision,
                            source_repo=source_repo, base_commit=base_commit, branch=stored_branch,
                            worktree=stored_worktree, after_seq=after_seq)
            db.execute("INSERT INTO dispatch_provenance(run_id,assignment_id,assignment_revision,source_repo,base_commit,branch,worktree,after_seq,created) VALUES(?,?,?,?,?,?,?,?,?)",
                       (run_id, assignment_id, assignment_revision, source_repo, base_commit, branch, worktree,
                        after_seq, _now()))
            return dict(run_id=run_id, assignment_id=assignment_id, assignment_revision=assignment_revision,
                        source_repo=source_repo, base_commit=base_commit, branch=branch,
                        worktree=worktree, after_seq=after_seq)

    def get_dispatch_provenance(self, run_id, assignment_id, assignment_revision):
        self._require_ids(run_id=run_id, assignment_id=assignment_id)
        if not isinstance(assignment_revision, int) or isinstance(assignment_revision, bool) or assignment_revision < 1:
            raise TeamError(400, "invalid_provenance", "Assignment revision must be a positive integer")
        with self._db() as db:
            self._run(db, run_id)
            row = db.execute("SELECT * FROM dispatch_provenance WHERE run_id=? AND assignment_id=? AND assignment_revision=?", (run_id, assignment_id, assignment_revision)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _present_attempt(attempt):
        if attempt.get("result") is not None:
            attempt["result"] = json.loads(attempt["result"])
        return attempt

    def post_message(self, run_id, actor, recipient_id, body, thread_id=None, assignment_id=None, kind="message"):
        self._require_ids(run_id=run_id, actor=actor, recipient_id=recipient_id)
        if thread_id is not None:
            self._require_ids(thread_id=thread_id)
        if assignment_id is not None:
            self._require_ids(assignment_id=assignment_id)
        if not isinstance(body, str) or not body.strip() or kind not in ("message", "question", "proposal", "challenge", "request", "advice"):
            raise TeamError(400, "invalid_message", "Message body and supported kind are required")
        with self._db() as db:
            run = self._run(db, run_id); self._writable(run); role = self._actor(db, run, actor)
            self._check_messages(db, run)
            if not db.execute("SELECT 1 FROM participants WHERE run_id=? AND id=?", (run_id, recipient_id)).fetchone():
                raise TeamError(400, "invalid_recipient", "Recipient must be a participant in this run")
            if assignment_id is not None and not db.execute(
                    "SELECT 1 FROM assignments WHERE run_id=? AND id=?", (run_id, assignment_id)).fetchone():
                raise TeamError(404, "assignment_not_found", "Assignment was not found in this run")
            if actor != run["lead"]:
                own = db.execute("SELECT id FROM assignments WHERE run_id=? AND participant_id=?", (run_id, actor)).fetchone()
                if thread_id:
                    participant = db.execute("SELECT 1 FROM events WHERE run_id=? AND thread_id=? AND (actor=? OR recipient=?)", (run_id, thread_id, actor, actor)).fetchone()
                    if not participant:
                        raise TeamError(403, "not_in_thread", "Workers may reply only in threads they started or were addressed in")
                elif not own or assignment_id != own[0]:
                    raise TeamError(403, "assignment_required", "Workers must start a thread tied to their assignment")
            if thread_id and not db.execute("SELECT 1 FROM events WHERE run_id=? AND (id=? OR thread_id=?)", (run_id, thread_id, thread_id)).fetchone():
                raise TeamError(404, "thread_not_found", "Thread was not found in this run")
            eid = self._event(db, run_id, kind, actor, body.strip(), recipient_id, thread_id, assignment_id)
            db.execute("UPDATE events SET thread_id=COALESCE(thread_id,?) WHERE id=?", (eid, eid))
            return eid

    def conclude(self, run_id, actor, thread_id, summary, rationale, action=None, needs_lead=False):
        self._require_ids(run_id=run_id, actor=actor, thread_id=thread_id)
        if not isinstance(summary, str) or not summary.strip() or not isinstance(rationale, str) or not rationale.strip() or (action is not None and not isinstance(action, str)) or not isinstance(needs_lead, bool):
            raise TeamError(400, "invalid_conclusion", "Conclusion summary and rationale are required")
        with self._db() as db:
            run = self._run(db, run_id); self._writable(run); role = self._actor(db, run, actor)
            if not db.execute("SELECT 1 FROM events WHERE run_id=? AND (id=? OR thread_id=?)", (run_id, thread_id, thread_id)).fetchone():
                raise TeamError(404, "thread_not_found", "Thread was not found in this run")
            if actor != run["lead"] and not db.execute("SELECT 1 FROM events WHERE run_id=? AND thread_id=? AND (actor=? OR recipient=?)", (run_id, thread_id, actor, actor)).fetchone():
                raise TeamError(403, "not_in_thread", "Only thread participants or the lead may conclude it")
            payload = json.dumps({"summary": summary, "rationale": rationale, "action": action, "needs_lead": bool(needs_lead)})
            return self._event(db, run_id, "conclusion", actor, payload,
                               recipient=run["lead"] if needs_lead else None, thread_id=thread_id)

    def set_phase(self, run_id, actor, phase, spec=None, approved=False):
        self._require_ids(run_id=run_id, actor=actor)
        if not isinstance(phase, str) or phase not in self.PHASES or phase == "complete" or (spec is not None and not isinstance(spec, str)) or not isinstance(approved, bool):
            raise TeamError(400, "invalid_phase", "Unsupported team phase")
        with self._db() as db:
            run = self._run(db, run_id); self._writable(run)
            self._actor(db, run, actor)
            if actor != run["lead"]:
                raise TeamError(403, "lead_required", "Only the stored lead may change phase or approve a specification")
            if phase == "build" and not (run["approved"] and (spec is None or spec == run["spec"])):
                raise TeamError(409, "spec_not_approved", "Build requires an approved specification")
            if approved and (phase != "specification" or not isinstance(spec, str) or not spec.strip()):
                raise TeamError(400, "invalid_approval", "Approval requires a nonempty specification phase")
            changed = spec is not None and spec != run["spec"]
            db.execute("UPDATE runs SET phase=?,spec=COALESCE(?,spec),approved=CASE WHEN ? THEN 1 WHEN ? THEN 0 ELSE approved END,updated=? WHERE id=?", (phase, spec, int(approved), int(changed), _now(), run_id))
            self._event(db, run_id, "phase", actor, json.dumps({"phase": phase, "spec": spec, "approved": bool(approved)}))

    def close_run(self, run_id, actor, summary=None):
        self._require_ids(run_id=run_id, actor=actor)
        if summary is not None and not isinstance(summary, str):
            raise TeamError(400, "invalid_summary", "Close summary must be a string")
        with self._db() as db:
            run = self._run(db, run_id)
            if run["closed"]:
                raise TeamError(409, "run_closed", "Closed team runs cannot be changed")
            self._actor(db, run, actor)
            if actor != run["lead"]:
                raise TeamError(403, "lead_required", "Only the stored lead may close the run")
            db.execute("UPDATE runs SET closed=1,phase='complete',updated=? WHERE id=?", (_now(), run_id))
            self._event(db, run_id, "closed", actor, summary or "")

    def reopen_run(self, run_id, actor, phase, summary=None):
        """Reopen a closed run for an explicitly requested follow-up."""
        self._require_ids(run_id=run_id, actor=actor)
        if not isinstance(phase, str) or phase not in self.PHASES or phase == "complete":
            raise TeamError(400, "invalid_phase", "Reopen requires a supported working phase")
        if summary is not None and not isinstance(summary, str):
            raise TeamError(400, "invalid_summary", "Reopen summary must be a string")
        with self._db() as db:
            run = self._run(db, run_id)
            self._actor(db, run, actor)
            if actor != run["lead"]:
                raise TeamError(403, "lead_required", "Only the stored lead may reopen the run")
            if not run["closed"]:
                raise TeamError(409, "run_open", "Only a closed run can be reopened")
            if phase == "build" and not run["approved"]:
                raise TeamError(409, "spec_not_approved", "Build requires an approved specification")
            db.execute("UPDATE runs SET closed=0,phase=?,updated=? WHERE id=?", (phase, _now(), run_id))
            self._event(db, run_id, "reopened", actor, json.dumps({"phase": phase, "summary": summary}))
        return self.get_run(run_id)

    def list_inbox(self, run_id, recipient, unread_only=False):
        self._require_ids(run_id=run_id, recipient=recipient)
        if not isinstance(unread_only, bool):
            raise TeamError(400, "invalid_inbox", "unread_only must be a boolean")
        with self._db() as db:
            run = self._run(db, run_id)
            self._actor(db, run, recipient)
            query = "SELECT e.*,d.acknowledged FROM deliveries d JOIN events e ON e.id=d.event_id WHERE e.run_id=? AND d.recipient=?"
            if unread_only:
                query += " AND d.acknowledged IS NULL"
            return [dict(row) for row in db.execute(query + " ORDER BY e.seq", (run_id, recipient))]

    def acknowledge_delivery(self, run_id, recipient, event_id):
        self._require_ids(run_id=run_id, recipient=recipient, event_id=event_id)
        with self._db() as db:
            run = self._run(db, run_id)
            self._actor(db, run, recipient)
            changed = db.execute("UPDATE deliveries SET acknowledged=COALESCE(acknowledged,?) WHERE event_id=? AND recipient=? AND event_id IN (SELECT id FROM events WHERE run_id=?)", (_now(), event_id, recipient, run_id)).rowcount
            if not changed:
                raise TeamError(404, "delivery_not_found", "Recipient delivery was not found")
            return db.execute("SELECT acknowledged FROM deliveries WHERE event_id=? AND recipient=?", (event_id, recipient)).fetchone()[0]

    def set_lead_summary(self, run_id, actor, summary):
        self._require_ids(run_id=run_id, actor=actor)
        if not isinstance(summary, str) or not summary.strip():
            raise TeamError(400, "invalid_summary", "Lead summary is required")
        with self._db() as db:
            run = self._run(db, run_id); self._writable(run); self._actor(db, run, actor)
            if actor != run["lead"]:
                raise TeamError(403, "lead_required", "Only the lead may update the run summary")
            return self._event(db, run_id, "summary", actor, summary.strip())

    def ask_user_question(self, run_id, actor, question):
        self._require_ids(run_id=run_id, actor=actor)
        if not isinstance(question, str) or not question.strip():
            raise TeamError(400, "invalid_question", "Question is required")
        with self._db() as db:
            run = self._run(db, run_id); self._writable(run); self._actor(db, run, actor)
            if actor != run["lead"]:
                raise TeamError(403, "lead_required", "Only the lead may route a question to the user")
            event_id = self._event(db, run_id, "user_question", actor, question.strip())
            db.execute("INSERT INTO user_questions(event_id) VALUES(?)", (event_id,))
            return event_id

    def resolve_user_question(self, run_id, actor, question_id, answer):
        self._require_ids(run_id=run_id, actor=actor, question_id=question_id)
        if not isinstance(answer, str) or not answer.strip():
            raise TeamError(400, "invalid_answer", "Question answer is required")
        with self._db() as db:
            run = self._run(db, run_id); self._writable(run); self._actor(db, run, actor)
            if actor != run["lead"]:
                raise TeamError(403, "lead_required", "Only the lead may record a user answer")
            row = db.execute("SELECT answer_event_id FROM user_questions WHERE event_id=? AND event_id IN (SELECT id FROM events WHERE run_id=?)", (question_id, run_id)).fetchone()
            if not row:
                raise TeamError(404, "question_not_found", "User question was not found")
            if row[0]:
                raise TeamError(409, "question_resolved", "User question already has an answer")
            answer_id = self._event(db, run_id, "user_answer", actor, answer.strip(), thread_id=question_id)
            db.execute("UPDATE user_questions SET answer_event_id=? WHERE event_id=?", (answer_id, question_id))
            return answer_id

    def add_artifact(self, run_id, actor, kind, title, uri):
        self._require_ids(run_id=run_id, actor=actor)
        if any(not isinstance(v, str) or not v.strip() for v in (kind, title, uri)):
            raise TeamError(400, "invalid_artifact", "Artifact kind, title, and URI are required")
        with self._db() as db:
            run = self._run(db, run_id); self._writable(run); self._actor(db, run, actor)
            artifact = {"id": _id(), "run_id": run_id, "actor": actor, "kind": kind.strip(),
                        "title": title.strip(), "uri": uri.strip(), "created": _now()}
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)", tuple(artifact.values()))
            self._event(db, run_id, "artifact", actor, json.dumps(artifact, ensure_ascii=False))
            return artifact

    def main_view(self, run_id):
        self._require_ids(run_id=run_id)
        run = self.get_run(run_id)
        events = run["events"]
        run["summary"] = next(({"text": e["body"], "updated": e["created"]} for e in reversed(events) if e["kind"] == "summary"), None)
        answered = {e["thread_id"] for e in events if e["kind"] == "user_answer"}
        run["questions"] = [{**e, "pending": True} for e in events
                            if e["kind"] == "user_question" and e["id"] not in answered]
        with self._db() as db:
            run["attempts"] = [self._present_attempt(dict(row)) for row in db.execute(
                "SELECT * FROM attempts WHERE run_id=? ORDER BY created,id", (run_id,))]
            inbox = {p["id"]: [] for p in run["participants"]}
            for row in db.execute(
                    "SELECT e.*, d.acknowledged, d.recipient AS delivery_recipient "
                    "FROM deliveries d JOIN events e ON e.id=d.event_id "
                    "WHERE e.run_id=? ORDER BY e.seq", (run_id,)):
                delivery = dict(row)
                inbox.setdefault(delivery.pop("delivery_recipient"), []).append(delivery)
            run["inbox"] = inbox
            return run
