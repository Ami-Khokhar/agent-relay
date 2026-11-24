"""Durable, local coordination records for Relay team runs."""
import json
import math
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


class TeamStore:
    PHASES = {"exploration", "prototype", "specification", "build", "review", "complete"}

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
                    updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    run_id TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL, recipient TEXT,
                    thread_id TEXT, assignment_id TEXT, body TEXT NOT NULL, created REAL NOT NULL);
            """)

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
        run_id, now = _id(), _now()
        with self._db() as db:
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)", (run_id, objective.strip(), lead, "exploration", None, 0, 0, json.dumps(limits), now, now))
            db.execute("INSERT INTO participants VALUES(?,?,?,?,?,?)", (run_id, lead, "lead", None, None, None))
            for p in roster:
                role = p.get("role", "worker")
                if not isinstance(role, str) or role not in ("worker", "reviewer", "evaluator"):
                    raise TeamError(400, "invalid_role", "Participant role must be worker, reviewer, or evaluator")
                db.execute("INSERT INTO participants VALUES(?,?,?,?,?,?)", (run_id, p["id"], role, p.get("harness"), p.get("model"), p.get("agentId")))
            self._event(db, run_id, "created", lead, objective.strip())
        return self.get_run(run_id)

    def list_runs(self):
        with self._db() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM runs ORDER BY created,id")]
        return [self.get_run(i) for i in ids]

    def get_run(self, run_id):
        with self._db() as db:
            r = dict(self._run(db, run_id))
            r["limits"] = json.loads(r["limits"])
            r["approved"] = bool(r["approved"]); r["closed"] = bool(r["closed"])
            r["participants"] = [dict(p) for p in db.execute("SELECT * FROM participants WHERE run_id=? ORDER BY id", (run_id,))]
            for participant in r["participants"]:
                participant["agentId"] = participant.pop("agent_id")
            r["assignments"] = [dict(a, dependencies=json.loads(a["dependencies"])) for a in db.execute("SELECT * FROM assignments WHERE run_id=? ORDER BY updated,id", (run_id,))]
            r["events"] = [dict(e) for e in db.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,))]
            return r

    def assign(self, run_id, actor, participant_id, assignment, dependencies=None):
        if not isinstance(assignment, str) or not assignment.strip():
            raise TeamError(400, "invalid_assignment", "Assignment text is required")
        if dependencies is not None and (not isinstance(dependencies, list) or any(not isinstance(d, str) for d in dependencies)):
            raise TeamError(400, "invalid_dependencies", "Dependencies must be a list of assignment IDs")
        with self._db() as db:
            run = self._run(db, run_id); self._writable(run)
            if self._actor(db, run, actor) != "lead":
                raise TeamError(403, "lead_required", "Only the stored lead may change assignments")
            if not db.execute("SELECT 1 FROM participants WHERE run_id=? AND id=?", (run_id, participant_id)).fetchone() or participant_id == run["lead"]:
                raise TeamError(400, "invalid_assignee", "Assignee must be a worker in this run")
            row = db.execute("SELECT id FROM assignments WHERE run_id=? AND participant_id=?", (run_id, participant_id)).fetchone()
            aid, now = (row[0] if row else _id()), _now()
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
            db.execute("INSERT OR REPLACE INTO assignments VALUES(?,?,?,?,?,?)", (aid, run_id, participant_id, assignment.strip(), json.dumps(dependencies or []), now))
            self._event(db, run_id, "assignment", actor, assignment.strip(), recipient=participant_id, assignment_id=aid)
            return aid

    def post_message(self, run_id, actor, recipient_id, body, thread_id=None, assignment_id=None, kind="message"):
        if not isinstance(body, str) or not body.strip() or kind not in ("message", "question", "proposal", "challenge", "request", "advice"):
            raise TeamError(400, "invalid_message", "Message body and supported kind are required")
        with self._db() as db:
            run = self._run(db, run_id); self._writable(run); role = self._actor(db, run, actor)
            self._check_messages(db, run)
            if not db.execute("SELECT 1 FROM participants WHERE run_id=? AND id=?", (run_id, recipient_id)).fetchone():
                raise TeamError(400, "invalid_recipient", "Recipient must be a participant in this run")
            if role != "lead":
                own = db.execute("SELECT id FROM assignments WHERE run_id=? AND participant_id=?", (run_id, actor)).fetchone()
                if thread_id:
                    participant = db.execute("SELECT 1 FROM events WHERE run_id=? AND thread_id=? AND (actor=? OR recipient=?)", (run_id, thread_id, actor, actor)).fetchone()
                    if not participant:
                        raise TeamError(403, "not_in_thread", "Workers may reply only in threads they started or were addressed in")
                elif not own or (assignment_id is not None and assignment_id != own[0]):
                    raise TeamError(403, "assignment_required", "Workers must start a thread tied to their assignment")
            if thread_id and not db.execute("SELECT 1 FROM events WHERE run_id=? AND (id=? OR thread_id=?)", (run_id, thread_id, thread_id)).fetchone():
                raise TeamError(404, "thread_not_found", "Thread was not found in this run")
            eid = self._event(db, run_id, kind, actor, body.strip(), recipient_id, thread_id, assignment_id)
            db.execute("UPDATE events SET thread_id=COALESCE(thread_id,?) WHERE id=?", (eid, eid))
            return eid

    def conclude(self, run_id, actor, thread_id, summary, rationale, action=None, needs_lead=False):
        if not isinstance(summary, str) or not summary.strip() or not isinstance(rationale, str) or not rationale.strip() or (action is not None and not isinstance(action, str)) or not isinstance(needs_lead, bool):
            raise TeamError(400, "invalid_conclusion", "Conclusion summary and rationale are required")
        with self._db() as db:
            run = self._run(db, run_id); self._writable(run); role = self._actor(db, run, actor)
            if not db.execute("SELECT 1 FROM events WHERE run_id=? AND (id=? OR thread_id=?)", (run_id, thread_id, thread_id)).fetchone():
                raise TeamError(404, "thread_not_found", "Thread was not found in this run")
            if role != "lead" and not db.execute("SELECT 1 FROM events WHERE run_id=? AND thread_id=? AND (actor=? OR recipient=?)", (run_id, thread_id, actor, actor)).fetchone():
                raise TeamError(403, "not_in_thread", "Only thread participants or the lead may conclude it")
            payload = json.dumps({"summary": summary, "rationale": rationale, "action": action, "needs_lead": bool(needs_lead)})
            return self._event(db, run_id, "conclusion", actor, payload, thread_id=thread_id)

    def set_phase(self, run_id, actor, phase, spec=None, approved=False):
        if not isinstance(phase, str) or phase not in self.PHASES or phase == "complete" or (spec is not None and not isinstance(spec, str)) or not isinstance(approved, bool):
            raise TeamError(400, "invalid_phase", "Unsupported team phase")
        with self._db() as db:
            run = self._run(db, run_id); self._writable(run)
            if self._actor(db, run, actor) != "lead":
                raise TeamError(403, "lead_required", "Only the stored lead may change phase or approve a specification")
            if phase == "build" and not (run["approved"] and (spec is None or spec == run["spec"])):
                raise TeamError(409, "spec_not_approved", "Build requires an approved specification")
            if approved and (phase != "specification" or not isinstance(spec, str) or not spec.strip()):
                raise TeamError(400, "invalid_approval", "Approval requires a nonempty specification phase")
            changed = spec is not None and spec != run["spec"]
            db.execute("UPDATE runs SET phase=?,spec=COALESCE(?,spec),approved=CASE WHEN ? THEN 1 WHEN ? THEN 0 ELSE approved END,updated=? WHERE id=?", (phase, spec, int(approved), int(changed), _now(), run_id))
            self._event(db, run_id, "phase", actor, json.dumps({"phase": phase, "spec": spec, "approved": bool(approved)}))

    def close_run(self, run_id, actor, summary=None):
        if summary is not None and not isinstance(summary, str):
            raise TeamError(400, "invalid_summary", "Close summary must be a string")
        with self._db() as db:
            run = self._run(db, run_id)
            if run["closed"]:
                raise TeamError(409, "run_closed", "Closed team runs cannot be changed")
            if self._actor(db, run, actor) != "lead":
                raise TeamError(403, "lead_required", "Only the stored lead may close the run")
            db.execute("UPDATE runs SET closed=1,phase='complete',updated=? WHERE id=?", (_now(), run_id))
            self._event(db, run_id, "closed", actor, summary or "")
