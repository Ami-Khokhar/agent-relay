"""Authenticated shared-lock drain for the safe update (#119, #156).

Public-HTTP tests over the real server: the drain closes admission against
otherwise-racing submissions, already accepted workflows keep producing child
tasks and drain without interruption, and the CLI consumer never touches any
service control but its three admin verbs and healthz.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import safe_update
import update_drain
from relay_helpers import TOKEN, Relay, send_json, start_http_server, stop_http_server

# Sleeps for the seconds the task's input names, then prints "drained".
SLEEPER = "import sys, time; time.sleep(float(sys.argv[-1])); print('drained')"
# Echoes its input, exactly as the workflow suite's echo agent does.
ECHO = "import sys; sys.stdout.write(sys.argv[1])"
# Fails the first run, then succeeds: the mark file is its memory, exactly as
# the workflow suite does it.
FLAKY = (
    "import os, sys, pathlib\n"
    "mark = pathlib.Path(os.environ['WF_MARK'])\n"
    "if mark.exists():\n"
    "    print('recovered')\n"
    "else:\n"
    "    mark.write_text('1')\n"
    "    sys.stderr.write('transient failure')\n"
    "    sys.exit(1)\n"
)
TERMINAL = ("completed", "failed", "cancelled")


def run_to_terminal(relay, workflow_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, view = relay.request("GET", f"/v1/workflows/{workflow_id}")
        if view.get("status") in TERMINAL:
            return view
        time.sleep(0.03)
    raise RuntimeError("workflow did not finish")


class DrainTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="a2a-relay-drain-")
        self.data = tempfile.mkdtemp(prefix="a2a-relay-drain-data-")
        self.relays = []

    def tearDown(self):
        for relay in self.relays:
            relay.close()
        shutil.rmtree(self.state, ignore_errors=True)
        shutil.rmtree(self.data, ignore_errors=True)

    def start(self, agents, env=None):
        relay = Relay(agents, env={"AGENT_RELAY_STATE_DIR": self.state, **(env or {})})
        self.relays.append(relay)
        return relay

    def path(self, name):
        return os.path.join(self.data, name)

    def sleeper(self, agent_id="slow"):
        return {"id": agent_id, "command": sys.executable, "args": ["-c", SLEEPER]}

    def echo(self, agent_id="echo"):
        return {"id": agent_id, "command": sys.executable, "args": ["-c", ECHO]}

    def flaky(self, agent_id="flaky"):
        return {"id": agent_id, "command": sys.executable, "args": ["-c", FLAKY],
                "env": {"WF_MARK": self.path("flaky-mark")}}

    # ----------------------------------------------------------------- shared lock

    def test_acquire_races_concurrent_admissions_and_closes_them_atomically(self):
        relay = self.start([self.sleeper()])
        # A request that will later be replayed exists before any drain, so
        # its semantics during a held drain can be observed honestly.
        status, prior = relay.submit(agentId="slow", input="1", requestId="replay-me")
        self.assertEqual(status, 202)
        results, lock = [], threading.Lock()

        def submit_round():
            for _ in range(8):
                result = relay.submit(agentId="slow", input="0.05")
                with lock:
                    results.append(result)
                time.sleep(0.01)

        threads = [threading.Thread(target=submit_round) for _ in range(4)]
        for thread in threads:
            thread.start()
        time.sleep(0.02)  # the submissions are mid-flight when the lock closes
        acquired = update_drain.acquire_drain(relay.base, "txn-a", token=TOKEN)
        self.assertTrue(acquired["capability"])
        for thread in threads:
            thread.join()
        self.assertEqual({(status, body.get("error")) for status, body in results},
                         {(202, None), (503, "drain_active")},
                         "each racing submission either fully lands before the "
                         "close or refuses as one clean drain refusal")
        self.assertFalse(acquired["quiesced"],
                         "an acquire is the hold, not a healthz guess")
        # A replay is not an admission: it keeps its identity and its 200,
        # even while the held drain refuses every fresh request.
        status, replayed = relay.submit(agentId="slow", input="1", requestId="replay-me")
        self.assertEqual((status, replayed["id"]), (200, prior["id"]))
        self.assertEqual(relay.submit(agentId="slow", input="1", requestId="other")[0],
                         503, "a fresh request is refused")
        # Conflicting requestId replays still conflict, not refuse as drain:
        self.assertEqual(relay.submit(agentId="slow", input="9", requestId="replay-me")
                         [0], 409)
        # The relay acknowledges quiescence itself from its own counts, and the
        # updater never decides it alone.
        final = update_drain.wait_for_quiescence(
            relay.base, "txn-a", token=TOKEN, revision=acquired["revision"],
            capability=acquired["capability"], wait_s=10)
        self.assertTrue(final["quiesced"])
        # Admission reopens through the explicit release only.
        self.assertEqual(relay.submit(agentId="slow", input="0.05")[0], 503)
        update_drain.release_drain(relay.base, "txn-a", token=TOKEN,
                                   revision=acquired["revision"],
                                   capability=acquired["capability"])
        self.assertEqual(relay.submit(agentId="slow", input="0.05")[0], 202,
                         "after the release, submissions work again")

    def test_conflict_and_capability_mismatch_are_bounded_refusals_never_idle(self):
        relay = self.start([self.sleeper()])
        acquired = update_drain.acquire_drain(relay.base, "txn-a", token=TOKEN)
        revision, capability = acquired["revision"], acquired["capability"]
        # A conflicting transaction is refused, bounded, and never reissued.
        with self.assertRaises(update_drain.DrainError) as conflict:
            update_drain.acquire_drain(relay.base, "txn-b", token=TOKEN, wait_s=0.4)
        self.assertEqual((conflict.exception.code, conflict.exception.status),
                         ("drain_held", 409))
        status, view = relay.request(
            "GET", "/v1/admin/update/drain?transactionId=txn-a&revision=%s&capability=%s"
            % (revision, capability))
        self.assertEqual(status, 200)
        self.assertNotIn("capability", view, "status gives no new token")
        # A replayed or foreign capability is an error, and no error reads idle.
        for verb, call in (
            ("status", lambda capability: update_drain.drain_status(
                relay.base, "txn-a", token=TOKEN, revision=revision,
                capability=capability)),
            ("release", lambda capability: update_drain.release_drain(
                relay.base, "txn-a", token=TOKEN, revision=revision,
                capability=capability)),
            ("release-other-transaction", lambda capability: update_drain.release_drain(
                relay.base, "txn-b", token=TOKEN, revision=revision,
                capability=capability))):
            with self.subTest(verb=verb):
                with self.assertRaises(update_drain.DrainError) as replayed:
                    call("0" * 64)
                self.assertEqual(replayed.exception.code, "capability_mismatch")
                self.assertEqual(replayed.exception.status, 403)
        # A leaked capability, in a body, does everything: never a channel.
        self.assertEqual(relay.request(
            "GET", "/v1/admin/update/drain?transactionId=txn-a&revision=%s"
            % revision)[0], 400)
        # A failed release leaves the hold exactly as it was: admission closed.
        self.assertEqual(relay.submit(agentId="slow", input="0.05")[0], 503)
        update_drain.release_drain(relay.base, "txn-a", token=TOKEN,
                                   revision=revision, capability=capability)
        self.assertEqual(relay.submit(agentId="slow", input="0.05")[0], 202)

    def test_exact_starting_revision_and_malformed_requests_always_refuse(self):
        relay = self.start([self.sleeper()])
        revision = update_drain._server_revision(relay.base)
        self.assertEqual(len(revision), 40)
        with self.assertRaises(update_drain.DrainError) as mismatch:
            update_drain.acquire_drain(relay.base, "txn-wrong", token=TOKEN,
                                       revision="0" * 40, wait_s=0.0)
        self.assertEqual((mismatch.exception.code, mismatch.exception.status),
                         ("revision_mismatch", 409))
        # Without any hold, a capability the relay never issued is never
        # answered as a free drain.
        with self.assertRaises(update_drain.DrainError) as nothing:
            update_drain.drain_status(relay.base, "txn-a", token=TOKEN,
                                      revision=revision, capability="0" * 64)
        self.assertEqual(nothing.exception.code, "drain_not_held")
        self.assertEqual(nothing.exception.status, 409)
        # A malformed request cannot buy any verdict: it never reads idle.
        self.assertEqual(relay.request("GET", "/v1/admin/update/drain",
                                       {"transactionId": "txn-a"})[0], 400)
        self.assertEqual(relay.request(
            "DELETE", "/v1/admin/update/drain",
            {"transactionId": "txn-a", "revision": "nothex",
             "capability": "0" * 64})[0], 400)
        self.assertEqual(relay.request(
            "POST", "/v1/admin/update/drain",
            {"transactionId": "a?", "revision": revision})[0], 400)

    # ---------------------------------------------------- workflows during a drain

    def test_accepted_workflows_continue_their_children_during_a_drain(self):
        """Already accepted runs keep going through a between-step gap, a retry
        delay and fanout, on the engine's own internal admission path, while
        every new external submission refuses."""
        relay = self.start([self.sleeper(), self.echo(), self.flaky()])
        status, submitted = relay.request("POST", "/v1/workflows", {"steps": [
            {"id": "first", "agentId": "flaky", "input": "attempt", "retries": 3},
            {"id": "second", "agentId": "echo", "input": "hello", "needs": ["first"]},
        ]})
        self.assertEqual(status, 202)
        # Hold the drain while the flaky attempt is in flight. That attempt
        # then fails, and its retry — a task created after the drain started —
        # submits through the trusted internal path and completes.
        acquired = update_drain.acquire_drain(relay.base, "txn-wf", token=TOKEN)
        # The accepted run still finishes and reports its whole path:
        view = run_to_terminal(relay, submitted["id"])
        self.assertEqual(view["status"], "completed")
        steps = {step["id"]: step for step in view["steps"]}
        self.assertEqual(steps["first"]["attempts"], 2, "the retry ran at all")
        self.assertEqual(steps["second"]["output"], "hello")
        # The children were not external admissions: the body had no chance
        # to opt out. A caller cannot send a flag to buy the same path.
        self.assertEqual(relay.request("POST", "/v1/workflows", {"steps": [
            {"id": "only", "agentId": "echo", "input": "bang"},
            {"parentTaskId": "opaque-bypass", "id": "else", "agentId": "echo",
             "input": "bang"}]})[0], 400, "a request field never bypasses the drain")
        self.assertEqual(relay.submit(agentId="echo", input="external")[0], 503)
        update_drain.release_drain(relay.base, "txn-wf", token=TOKEN,
                                   revision=acquired["revision"],
                                   capability=acquired["capability"])

    def test_new_workflow_submit_and_resume_refuse_while_the_drain_is_held(self):
        relay = self.start([self.sleeper(), self.echo()])
        status, submitted = relay.request("POST", "/v1/workflows", {"steps": [
            {"id": "work", "agentId": "slow", "input": "4"}]})
        self.assertEqual(status, 202)
        # Stop the run on request, exactly as the kill button does, so a
        # journal exists for a later resume.
        status, killed = relay.request("POST", "/v1/pressure/kill")
        self.assertEqual(status, 200)
        self.assertIn(submitted["id"], killed["killed"])
        acquired = update_drain.acquire_drain(relay.base, "txn-r", token=TOKEN)
        self.assertEqual(relay.request(
            "POST", "/v1/workflows", {"steps": [{"id": "x", "agentId": "echo",
                                                 "input": "bang"}]})[0], 503)
        self.assertEqual(relay.request(
            "POST", f"/v1/workflows/{submitted['id']}/resume")[0], 503,
            "resume is refused atomically too")
        update_drain.release_drain(relay.base, "txn-r", token=TOKEN,
                                   revision=acquired["revision"],
                                   capability=acquired["capability"])
        self.assertEqual(relay.request(
            "POST", f"/v1/workflows/{submitted['id']}/resume")[0], 202,
            "after the release, resumes work again")

    # -------------------------------------------------- active slots in the count

    def test_cancelled_http_adapter_that_still_runs_is_counted_and_waits(self):
        release_adapter = threading.Event()
        started = threading.Event()

        def handler(request):
            length = int(request.headers.get("content-length", 0))
            request.rfile.read(length)
            started.set()
            release_adapter.wait(5)
            send_json(request, 200, {"protocolVersion": "relay.adapter/v1",
                                     "status": "completed", "output": "late"})

        server, port = start_http_server(handler)
        relay = self.start([{"id": "hosted", "type": "http",
                             "url": "http://127.0.0.1:%d" % port}])
        try:
            status, submitted = relay.submit(agentId="hosted", input="hold")
            self.assertEqual(status, 202)
            self.assertTrue(started.wait(5))
            self.assertEqual(relay.request("DELETE",
                                           f"/v1/tasks/{submitted['id']}")[0], 200)
            acquired = update_drain.acquire_drain(relay.base, "txn-c", token=TOKEN)
            self.assertFalse(acquired["quiesced"],
                             "the cancelled adapter still owns its slot")
            self.assertEqual(acquired["active"], 1)
            release_adapter.set()
            final = update_drain.wait_for_quiescence(
                relay.base, "txn-c", token=TOKEN, revision=acquired["revision"],
                capability=acquired["capability"], wait_s=8)
            self.assertTrue(final["quiesced"],
                            "once the slot finally frees, the held drain reports it")
            update_drain.release_drain(relay.base, "txn-c", token=TOKEN,
                                       revision=acquired["revision"],
                                       capability=acquired["capability"])
            self.assertEqual(relay.submit(agentId="hosted", input="0")[0], 202)
        finally:
            stop_http_server(server)

    # --------------------------------------------------------------- CLI consumer

    def test_drain_cli_does_the_rehearsal_and_touches_no_service_control(self):
        called = []
        real_request = update_drain._request

        def recording(method, base_url, path, **kwargs):
            called.append(path)
            if path != "/healthz":
                self.assertTrue((kwargs.get("token") or "").strip(),
                                "each admin call carries its authentication bearer")
            return real_request(method, base_url, path, **kwargs)

        relay = self.start([self.sleeper()])
        # A slow task makes the rehearsal truly wait for admitted work to end.
        status, busy = relay.submit(agentId="slow", input="0.01")
        self.assertEqual(status, 202)
        with patch.object(safe_update, "_stage_receipt", self._fake_stage_success), \
                patch.object(safe_update, "_consumed_consent", self._fake_consent), \
                patch.object(update_drain, "_request", recording), \
                patch.dict(os.environ, {"AGENT_RELAY_TOKEN": TOKEN}):
            self.assertEqual(
                safe_update.cmd_drain("0.4.0", os.getcwd(), base_url=relay.base,
                                      transaction_id="cli-txn", quiesce_wait_s=15), 0)
        self.assertEqual(called[0], "/healthz")
        for path in called[1:]:
            self.assertEqual(path, "/v1/admin/update/drain",
                             "the CLI touches no other endpoint but its three verbs")
        # The hold is gone: the relay opens again.
        self.assertEqual(relay.submit(agentId="slow", input="0.05")[0], 202)

    def test_drain_cli_reports_a_timeout_and_still_releases(self):
        release_adapter = threading.Event()
        started = threading.Event()

        def handler(request):
            length = int(request.headers.get("content-length", 0))
            request.rfile.read(length)
            started.set()
            release_adapter.wait(5)
            send_json(request, 200, {"protocolVersion": "relay.adapter/v1",
                                     "status": "completed", "output": "late"})

        server, port = start_http_server(handler)
        relay = self.start([{"id": "hosted", "type": "http",
                             "url": "http://127.0.0.1:%d" % port}])
        try:
            status, submitted = relay.submit(agentId="hosted", input="hold")
            self.assertEqual(status, 202)
            self.assertTrue(started.wait(5))
            with patch.object(safe_update, "_stage_receipt",
                              self._fake_stage_failure), \
                    patch.object(safe_update, "_consumed_consent", self._fake_consent), \
                    patch.dict(os.environ, {"AGENT_RELAY_TOKEN": TOKEN}):
                self.assertEqual(
                    safe_update.cmd_drain("0.4.0", os.getcwd(), base_url=relay.base,
                                          transaction_id="cli-slow", quiesce_wait_s=0.5), 1)
            release_adapter.set()
            # Even a failed rehearsal releases: the hold never spills out of it.
            self.assertEqual(relay.submit(agentId="hosted", input="0")[0], 202)
        finally:
            stop_http_server(server)

    def test_drain_cli_reports_a_lost_hold_without_traceback(self):
        """Guard: a status refusal mid-rehearsal is a readable failure, not an AttributeError.

        wait_for_quiescence can raise a DrainError that is not the timeout
        instance (drain_not_held after a relay restart, capability_mismatch,
        drain_unreachable past the deadline); such an error carries no last
        acknowledged counts, and cmd_drain must report the failure and still
        release instead of printing a traceback.
        """
        # Not the timeout instance: no last_status travels with it.
        refused = update_drain.DrainError("drain_not_held",
                                          "the relay restarted mid-rehearsal")

        def failing_status(*args, **kwargs):
            raise refused

        server, port = start_http_server(lambda request: send_json(request, 200, {
            "protocolVersion": "relay.adapter/v1",
            "status": "completed", "output": "unused"}))
        relay = self.start([{"id": "hosted", "type": "http",
                             "url": "http://127.0.0.1:%d" % port}])
        try:
            self.assertEqual(relay.submit(agentId="hosted", input="x")[0], 202)
            with patch.object(safe_update, "_stage_receipt",
                              self._fake_stage_success), \
                    patch.object(safe_update, "_consumed_consent",
                                 self._fake_consent), \
                    patch.dict(os.environ, {"AGENT_RELAY_TOKEN": TOKEN}), \
                    patch.object(update_drain, "drain_status", failing_status):
                # DrainError without last_status: report it, return 1, and do
                # not raise, so the release warning path is the last thing the
                # caller sees.
                self.assertEqual(
                    safe_update.cmd_drain("0.4.0", os.getcwd(), base_url=relay.base,
                                          transaction_id="cli-lost", quiesce_wait_s=1), 1)
            # The hold is gone: the relay opens again.
            self.assertEqual(relay.submit(agentId="hosted", input="0.05")[0], 202)
        finally:
            stop_http_server(server)

    def _fake_stage_success(self, tag, install_dir, state_dir):
        return {}, {"tag": tag}

    def _fake_stage_failure(self, tag, install_dir, state_dir):
        return {}, {"tag": tag}

    def _fake_consent(self, identity, state_dir):
        return {}


if __name__ == "__main__":
    unittest.main()
