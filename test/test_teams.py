import json
import sqlite3

import os
import tempfile
import unittest
import sys
import time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from teams import TeamError, TeamStore


def team(path):
    store = TeamStore(path)
    run = store.create_run("Ship a useful change", "lead", [
        {"id": "worker-a", "role": "worker", "harness": "claude", "model": "m1", "agentId": "configured-a"},
        {"id": "worker-b", "role": "reviewer", "harness": "codex", "model": "m2"},
    ], {"max_concurrency": 2})
    return store, run["id"]


class TeamTests(unittest.TestCase):
 def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
 def path(self):
    return os.path.join(self.temp.name, "teams.db")
 def test_run_and_ordered_records_survive_restart(self):
    path = self.path()
    store, rid = team(path)
    aid = store.assign(rid, "lead", "worker-a", "Implement the feature")
    first = store.post_message(rid, "worker-a", "worker-b", "Can you review this?", assignment_id=aid, kind="request")
    store.post_message(rid, "worker-b", "worker-a", "I will inspect it", thread_id=first)

    reopened = TeamStore(path)
    run = reopened.get_run(rid)
    assert run["limits"] == {"max_concurrency": 2}
    assert [(p["id"], p["harness"], p["model"]) for p in run["participants"] if p["role"] != "lead"] == [
        ("worker-a", "claude", "m1"), ("worker-b", "codex", "m2")]
    self.assertEqual(next(p["agentId"] for p in run["participants"] if p["id"] == "worker-a"), "configured-a")
    assert run["assignments"][0]["id"] == aid
    assert [e["seq"] for e in run["events"]] == sorted(e["seq"] for e in run["events"])
    assert run["events"][-1]["thread_id"] == first
    assert reopened.list_runs()[0]["id"] == rid


 def test_worker_cannot_reassign_or_elevate_by_claiming_role(self):
    store, rid = team(self.path())
    with self.assertRaises(TeamError) as err:
        store.assign(rid, "worker-a", "worker-b", "Take over")
    assert (err.exception.status, err.exception.code) == (403, "lead_required")
    with self.assertRaises(TeamError) as err:
        store.assign(rid, "stranger", "worker-b", "Take over")
    assert err.exception.code == "not_participant"
    assert store.get_run(rid)["assignments"] == []
    store.assign(rid, "lead", "worker-a", "Implement")
    with self.assertRaises(TeamError) as err:
        store.post_message(rid, "worker-a", "worker-b", "Unrelated thread", thread_id="made-up")
    self.assertEqual(err.exception.code, "not_in_thread")


 def test_addressed_thread_conclusion_records_rationale(self):
    store, rid = team(self.path())
    aid = store.assign(rid, "lead", "worker-a", "Implement")
    root = store.post_message(rid, "worker-a", "worker-b", "Please review", assignment_id=aid)
    store.post_message(rid, "worker-b", "worker-a", "Looks good", thread_id=root, kind="advice")
    cid = store.conclude(rid, "worker-b", root, "Keep the implementation", "Review found no defect", "worker-a keeps ownership")
    conclusion = next(e for e in store.get_run(rid)["events"] if e["id"] == cid)
    assert conclusion["kind"] == "conclusion"
    assert json.loads(conclusion["body"]) == {
        "summary": "Keep the implementation", "rationale": "Review found no defect",
        "action": "worker-a keeps ownership", "needs_lead": False}


 def test_build_requires_lead_approved_spec_and_close_stops_mutation(self):
    store, rid = team(self.path())
    with self.assertRaises(TeamError) as err:
        store.set_phase(rid, "lead", "build")
    assert err.exception.code == "spec_not_approved"
    with self.assertRaises(TeamError) as err:
        store.set_phase(rid, "worker-a", "specification", "spec", approved=True)
    assert err.exception.code == "lead_required"
    store.set_phase(rid, "lead", "specification", "Acceptance: works", approved=True)
    store.set_phase(rid, "lead", "build")
    assert store.get_run(rid)["phase"] == "build"
    store.set_phase(rid, "lead", "specification", "Revised acceptance", approved=False)
    assert store.get_run(rid)["approved"] is False
    with self.assertRaises(TeamError) as err:
        store.set_phase(rid, "lead", "build")
    self.assertEqual(err.exception.code, "spec_not_approved")
    store.set_phase(rid, "lead", "specification", "Revised acceptance", approved=True)
    store.close_run(rid, "lead", "Delivered")
    assert store.get_run(rid)["closed"] is True
    with self.assertRaises(TeamError) as err:
        store.post_message(rid, "worker-a", "worker-b", "More work")
    assert err.exception.code == "run_closed"
    with self.assertRaises(TeamError) as err:
        store.assign(rid, "lead", "worker-a", "Continue")
    assert err.exception.code == "run_closed"

 def test_limits_validation_and_expired_run_can_still_be_closed(self):
    with self.assertRaises(TeamError) as err:
        TeamStore(self.path()).create_run("goal", "lead", [{"id": []}], {})
    self.assertEqual(err.exception.code, "invalid_roster")
    with self.assertRaises(TeamError) as err:
        TeamStore(self.path()).create_run("goal", "lead", [], {"max_messages": float("inf")})
    self.assertEqual(err.exception.code, "invalid_limits")
    store = TeamStore(self.path())
    rid = store.create_run("goal", "lead", [], {"max_elapsed_seconds": 1})["id"]
    with store._db() as db:
        db.execute("UPDATE runs SET created=? WHERE id=?", (time.time() - 2, rid))
    with self.assertRaises(TeamError) as err:
        store.set_phase(rid, "lead", "prototype")
    self.assertEqual(err.exception.code, "time_limit")
    store.close_run(rid, "lead", "Stopped after limit")
    self.assertTrue(store.get_run(rid)["closed"])

 def test_dependencies_must_exist_and_cannot_cycle(self):
    store, rid = team(self.path())
    with self.assertRaises(TeamError) as err:
        store.assign(rid, "lead", "worker-a", "A", ["missing"])
    self.assertEqual(err.exception.code, "invalid_dependencies")
    first = store.assign(rid, "lead", "worker-a", "A")
    second = store.assign(rid, "lead", "worker-b", "B", [first])
    with self.assertRaises(TeamError) as err:
        store.assign(rid, "lead", "worker-a", "A revised", [second])
    self.assertEqual(err.exception.code, "invalid_dependencies")

 def test_message_budget_is_enforced_and_assignment_count_is_not_execution_concurrency(self):
    store = TeamStore(self.path())
    rid = store.create_run("goal", "lead", [{"id": "a"}, {"id": "b"}],
                           {"max_messages": 1, "max_concurrency": 1})["id"]
    store.assign(rid, "lead", "a", "A")
    store.assign(rid, "lead", "b", "B")
    store.post_message(rid, "lead", "a", "One message")
    with self.assertRaises(TeamError) as err:
        store.post_message(rid, "lead", "b", "Over budget")
    self.assertEqual(err.exception.code, "message_limit")

 def test_recipient_delivery_ack_is_durable_and_does_not_create_an_answer(self):
    path = self.path()
    store, rid = team(path)
    event_id = store.post_message(rid, "lead", "worker-a", "Please inspect", kind="request")
    self.assertEqual([e["id"] for e in store.list_inbox(rid, "worker-a", unread_only=True)], [event_id])
    acknowledged = store.acknowledge_delivery(rid, "worker-a", event_id)
    reopened = TeamStore(path)
    self.assertEqual(reopened.list_inbox(rid, "worker-a", unread_only=True), [])
    self.assertEqual(reopened.list_inbox(rid, "worker-a")[0]["acknowledged"], acknowledged)
    self.assertEqual(len(reopened.get_run(rid)["events"]), 2)

 def test_main_view_exposes_lead_questions_only_until_answered(self):
    store, rid = team(self.path())
    aid = store.assign(rid, "lead", "worker-a", "Review API")
    store.set_lead_summary(rid, "lead", "Prototype is ready")
    store.post_message(rid, "worker-a", "lead", "Should we keep the API?", assignment_id=aid, kind="question")
    user_question = store.ask_user_question(rid, "lead", "Keep the current API shape?")
    artifact = store.add_artifact(rid, "lead", "prototype", "Preview", "file:///preview")
    view = store.main_view(rid)
    self.assertEqual(view["summary"]["text"], "Prototype is ready")
    self.assertEqual([q["body"] for q in view["questions"]], ["Keep the current API shape?"])
    self.assertTrue(view["questions"][0]["pending"])
    self.assertEqual(view["artifacts"][0]["id"], artifact["id"])
    self.assertIn("worker-a", view["inbox"])
    store.resolve_user_question(rid, "lead", user_question, "Keep it")
    self.assertEqual(store.main_view(rid)["questions"], [])

 def test_delivery_acknowledgment_cannot_cross_run_boundary(self):
    store, first = team(self.path())
    event_id = store.post_message(first, "lead", "worker-a", "For first run")
    second = store.create_run("other", "lead", [{"id": "worker-a"}])["id"]
    with self.assertRaises(TeamError) as err:
        store.acknowledge_delivery(second, "worker-a", event_id)
    self.assertEqual(err.exception.code, "delivery_not_found")

 def test_schema_upgrade_preserves_existing_run_and_assignment_records(self):
    path = self.path()
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE runs(id TEXT PRIMARY KEY, objective TEXT NOT NULL, lead TEXT NOT NULL, phase TEXT NOT NULL,
            spec TEXT, approved INTEGER NOT NULL, closed INTEGER NOT NULL, limits TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE participants(run_id TEXT NOT NULL, id TEXT NOT NULL, role TEXT NOT NULL, harness TEXT, model TEXT, PRIMARY KEY(run_id,id));
        CREATE TABLE assignments(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, participant_id TEXT NOT NULL, text TEXT NOT NULL, dependencies TEXT NOT NULL, updated REAL NOT NULL);
        CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, run_id TEXT NOT NULL,
            kind TEXT NOT NULL, actor TEXT NOT NULL, recipient TEXT, thread_id TEXT, assignment_id TEXT, body TEXT NOT NULL, created REAL NOT NULL);
        INSERT INTO runs VALUES('legacy-run','old goal','lead','exploration',NULL,0,0,'{}',1,1);
        INSERT INTO participants VALUES('legacy-run','lead','lead',NULL,NULL);
        INSERT INTO participants VALUES('legacy-run','worker','worker',NULL,NULL);
        INSERT INTO assignments VALUES('legacy-assignment','legacy-run','worker','keep this','[]',2);
        INSERT INTO events(id,run_id,kind,actor,recipient,body,created) VALUES('legacy-event','legacy-run','request','lead','worker','still here',3);
    """)
    db.commit()
    db.close()
    store = TeamStore(path)
    run = store.get_run("legacy-run")
    self.assertEqual(run["assignments"][0]["text"], "keep this")
    self.assertEqual(run["assignments"][0]["revision"], 1)
    self.assertEqual(run["events"][0]["id"], "legacy-event")
    self.assertEqual(store.list_inbox("legacy-run", "worker")[0]["id"], "legacy-event")
