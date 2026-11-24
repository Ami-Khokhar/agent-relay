import json

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
