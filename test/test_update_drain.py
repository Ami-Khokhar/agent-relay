"""Authenticated shared-lock drain for the safe update (#119, #156).

Public-HTTP tests over the real server: the drain closes admission against
otherwise-racing submissions, already accepted workflows keep producing child
tasks and drain without interruption, and the CLI consumer never touches any
service control but its three admin verbs and healthz.
"""
from __future__ import annotations

import contextlib
import io
import json
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
import update_staging
import update_transaction
from pathlib import Path
from relay_helpers import TOKEN, Relay, send_json, start_http_server, stop_http_server
import release_identity as identity
# The real staging fixture: one real checkout, real git, real receipts. Reused
# here so the CLI consumer's verification runs against a genuine stage instead
# of a fake only this file could believe in.
import test_update_staging as staging_fixture

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
        outcomes = [(status, body.get("error")) for status, body in results]
        self.assertLessEqual(set(outcomes), {(202, None), (503, "drain_active")},
                             "each racing submission either fully lands before the "
                             "close or refuses as one clean drain refusal")
        # Whether a given submission lands is a scheduling race; that admission
        # closed is not. The round runs for many multiples of the 20ms head
        # start, so at least one submission meets the held drain.
        self.assertIn((503, "drain_active"), outcomes,
                      "submissions racing a held drain are refused, not all admitted")
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

    def test_a_lost_acquire_response_is_recovered_by_the_same_transaction(self):
        """The relay applied the acquire; its answer never arrived. Naming the
        same transaction again returns that hold's capability, so the operator
        releases it and admission reopens without restarting the relay."""
        relay = self.start([self.sleeper()])
        real_request = update_drain._request
        lost = []

        def losing(method, base_url, path, **kwargs):
            status, body = real_request(method, base_url, path, **kwargs)
            if method == "POST" and not lost:
                # The server minted the hold; the response is lost in transit,
                # exactly as a client timeout sees it.
                lost.append((status, body))
                raise update_drain.DrainError("drain_unreachable",
                                              "the acquire answer never arrived")
            return status, body

        with patch.object(update_drain, "_request", losing):
            acquired = update_drain.acquire_drain(relay.base, "txn-lost", token=TOKEN,
                                                  wait_s=10)
        self.assertEqual(len(lost), 1, "the first acquire did reach the relay")
        self.assertEqual(lost[0][0], 200)
        self.assertIs(lost[0][1]["reacquired"], False, "the lost answer created the hold")
        self.assertIs(lost[0][1]["recovered"], False, "nothing was recovered there")
        self.assertEqual(acquired["capability"], lost[0][1]["capability"],
                         "the retry receives the capability the lost answer carried")
        self.assertIs(acquired["reacquired"], True,
                      "the retry is a replay at the HTTP level")
        self.assertIs(acquired["recovered"], True,
                      "the retry recovered this very call's own lost answer")
        self.assertIs(acquired["createdHere"], True,
                      "this acquire call created the hold, whatever the last answer says")
        self.assertEqual(acquired["transactionId"], "txn-lost")
        # That very hold closed admission, and only its release reopens it.
        self.assertEqual(relay.submit(agentId="slow", input="0.05")[0], 503)
        update_drain.release_drain(relay.base, "txn-lost", token=TOKEN,
                                   revision=acquired["revision"],
                                   capability=acquired["capability"])
        self.assertEqual(relay.submit(agentId="slow", input="0.05")[0], 202,
                         "the recovered capability reopened admission, no restart")

    def test_an_unreachable_relay_still_names_how_to_recover_a_possible_hold(self):
        """Every retry may time out after the acquire landed. The refusal then
        says how to receive that hold's capability rather than only reporting
        the transport failure, and says where a hold no live process owns ends."""
        relay = self.start([self.sleeper()])
        real_request = update_drain._request
        applied = []

        def losing(method, base_url, path, **kwargs):
            if method == "POST":
                if not applied:
                    real_request(method, base_url, path, **kwargs)  # lands, answer lost
                    applied.append(True)
                raise update_drain.DrainError("drain_unreachable", "no answer")
            return real_request(method, base_url, path, **kwargs)

        with patch.object(update_drain, "_request", losing):
            with self.assertRaises(update_drain.DrainError) as unreachable:
                update_drain.acquire_drain(relay.base, "txn-dark", token=TOKEN, wait_s=0.3)
        self.assertEqual(unreachable.exception.code, "drain_unreachable")
        self.assertIn("--transaction-id txn-dark", str(unreachable.exception))
        # The abandoned hold is still there. A later call that did not create it
        # is shown the hold but never its capability: releasing is the creating
        # call's act, and the holder's name is public in the 409.
        recovered = update_drain.acquire_drain(relay.base, "txn-dark", token=TOKEN)
        self.assertIs(recovered["reacquired"], True)
        self.assertIs(recovered["recovered"], False,
                      "a later call never recovered its own attempt")
        self.assertIs(recovered["createdHere"], False,
                      "a later call treats the abandoned hold as not its own")
        self.assertNotIn("capability", recovered,
                         "a foreign caller is told the hold, never the release")
        with self.assertRaises(update_drain.DrainError):
            update_drain.release_drain(relay.base, "txn-dark", token=TOKEN,
                                       revision=recovered["revision"],
                                       capability="0" * 64)
        self.assertEqual(relay.submit(agentId="slow", input="0.05")[0], 503,
                         "the orphaned hold keeps admission closed until a restart")

    def test_a_foreign_hold_still_refuses_and_names_the_holder_and_release(self):
        relay = self.start([self.sleeper()])
        held = update_drain.acquire_drain(relay.base, "txn-owner", token=TOKEN)
        with self.assertRaises(update_drain.DrainError) as refused:
            update_drain.acquire_drain(relay.base, "txn-waiter", token=TOKEN, wait_s=0.3)
        error = refused.exception
        self.assertEqual((error.code, error.status), ("drain_held", 409))
        text = str(error)
        self.assertIn("txn-owner", text, "the refusal names who holds it")
        self.assertIn("--transaction-id txn-owner", text,
                      "it says how that hold can be inspected again")
        self.assertIn("only inspects", text,
                      "it never presents the repeat as a release or a takeover")
        self.assertIn("recover-abort txn-owner", text,
                      "it names the command that releases a prepared transaction")
        self.assertIn("ends only when the relay restarts", text,
                      "it names the one ending left when the owner is gone")
        # The refusal took nothing over: the owner's hold is intact.
        self.assertTrue(update_drain.drain_status(
            relay.base, "txn-owner", token=TOKEN, revision=held["revision"],
            capability=held["capability"])["held"])
        self.assertEqual(relay.submit(agentId="slow", input="0.05")[0], 503)
        update_drain.release_drain(relay.base, "txn-owner", token=TOKEN,
                                   revision=held["revision"],
                                   capability=held["capability"])
        self.assertEqual(relay.submit(agentId="slow", input="0.05")[0], 202)

    def test_the_cli_prints_the_release_path_of_a_foreign_hold(self):
        relay = self.start([self.sleeper()])
        held = update_drain.acquire_drain(relay.base, "txn-owner", token=TOKEN)
        real_acquire = update_drain.acquire_drain

        def bounded_acquire(*args, **kwargs):
            kwargs.setdefault("wait_s", 0.3)
            return real_acquire(*args, **kwargs)

        printed = io.StringIO()
        try:
            with patch.object(safe_update, "_stage_receipt", self._fake_stage_success), \
                    patch.object(safe_update, "_consumed_consent", self._fake_consent), \
                    patch.object(update_drain, "acquire_drain", bounded_acquire), \
                    patch.dict(os.environ, {"AGENT_RELAY_TOKEN": TOKEN}), \
                    contextlib.redirect_stderr(printed):
                self.assertEqual(safe_update.main([
                    "drain", "0.4.0", "--install-dir", os.getcwd(),
                    "--base-url", relay.base, "--transaction-id", "txn-waiter"]), 1)
        finally:
            update_drain.release_drain(relay.base, "txn-owner", token=TOKEN,
                                       revision=held["revision"],
                                       capability=held["capability"])
        message = printed.getvalue()
        self.assertIn("txn-owner", message)
        self.assertIn("recover-abort txn-owner", message,
                      "the CLI prints the command that releases the hold")

    def test_a_replayed_rehearsal_leaves_the_holders_hold_and_admission_closed(self):
        """Repeating `drain --transaction-id <holder>` is exactly what the refusal
        names. The relay answers that replay, so the rehearsal must report it and
        leave the hold alone instead of releasing a live activation's drain."""
        relay = self.start([self.sleeper()])
        held = update_drain.acquire_drain(relay.base, "txn-owner", token=TOKEN)
        printed = io.StringIO()
        with patch.object(safe_update, "_stage_receipt", self._fake_stage_success), \
                patch.object(safe_update, "_consumed_consent", self._fake_consent), \
                patch.dict(os.environ, {"AGENT_RELAY_TOKEN": TOKEN}), \
                contextlib.redirect_stdout(printed):
            self.assertEqual(safe_update.main([
                "drain", "0.4.0", "--install-dir", os.getcwd(),
                "--base-url", relay.base, "--transaction-id", "txn-owner"]), 1,
                "a replayed rehearsal did not rehearse anything")
        message = printed.getvalue()
        self.assertIn("did not create", message)
        self.assertIn("recover-abort txn-owner", message)
        self.assertNotIn("Drain released", message,
                         "a replay is never released by the command that repeated it")
        # Nothing reopened admission: the live hold is still exactly the one there.
        self.assertEqual(relay.submit(agentId="slow", input="0.05")[0], 503)
        self.assertTrue(update_drain.drain_status(
            relay.base, "txn-owner", token=TOKEN, revision=held["revision"],
            capability=held["capability"])["held"],
            "the owner's capability still owns the same hold")
        update_drain.release_drain(relay.base, "txn-owner", token=TOKEN,
                                   revision=held["revision"],
                                   capability=held["capability"])
        self.assertEqual(relay.submit(agentId="slow", input="0.05")[0], 202,
                         "only the owner's release reopens admission")

    def test_a_rehearsal_that_lost_its_own_acquire_answer_still_releases(self):
        """The reviewer's sequence: the first POST lands, its answer is lost, the
        retry is answered `reacquired: true`, and the command must still release
        the hold its own acquire call created instead of stranding admission."""
        relay = self.start([self.sleeper()])
        real_request = update_drain._request
        lost = []

        def losing(method, base_url, path, **kwargs):
            status, body = real_request(method, base_url, path, **kwargs)
            if method == "POST" and not lost:
                lost.append(body)
                raise update_drain.DrainError("drain_unreachable",
                                              "the acquire answer never arrived")
            return status, body

        printed = io.StringIO()
        with patch.object(safe_update, "_stage_receipt", self._fake_stage_success), \
                patch.object(safe_update, "_consumed_consent", self._fake_consent), \
                patch.object(update_drain, "_request", losing), \
                patch.dict(os.environ, {"AGENT_RELAY_TOKEN": TOKEN}), \
                contextlib.redirect_stdout(printed):
            self.assertEqual(safe_update.main([
                "drain", "0.4.0", "--install-dir", os.getcwd(),
                "--base-url", relay.base, "--transaction-id", "txn-own"]), 0,
                "the rehearsal ran on the hold its own acquire call created")
        self.assertEqual(len(lost), 1, "the first acquire did reach the relay")
        self.assertIs(lost[0]["reacquired"], False)
        message = printed.getvalue()
        self.assertIn("Drain txn-own held", message)
        self.assertIn("Drain released", message,
                      "the command released the hold it created, even though the "
                      "last answer replayed it")
        self.assertEqual(relay.submit(agentId="slow", input="0.05")[0], 202,
                         "admission reopened without a relay restart")

    def test_a_port_answering_a_non_object_revision_is_a_named_refusal(self):
        current = {}
        server, port = start_http_server(
            lambda request: send_json(request, 200, current["body"]))
        base = "http://127.0.0.1:%d" % port
        try:
            for body in ({"revision": "abc"}, {"revision": {"commit": 5}},
                         {"revision": ["abc"]}, {"revision": None}):
                with self.subTest(revision=body["revision"]):
                    current["body"] = body
                    with self.assertRaises(update_drain.DrainError) as refused:
                        update_drain._server_revision(base)
                    self.assertEqual(refused.exception.code, "drain_revision_unknown",
                                     "a foreign answer is a named refusal, never "
                                     "an AttributeError")
        finally:
            stop_http_server(server)

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
        # The creating call's name is validated like the transaction id: a
        # malformed one is a refusal that creates nothing.
        for bad in ("a?", "x" * 129, 5, ["a"]):
            status, body = relay.request("POST", "/v1/admin/update/drain",
                                         {"transactionId": "txn-a",
                                          "revision": revision, "requestId": bad})
            self.assertEqual((status, body.get("error")),
                             (400, "invalid_request_id"), bad)
        with self.assertRaises(update_drain.DrainError) as nothing_created:
            update_drain.drain_status(relay.base, "txn-a", token=TOKEN,
                                      revision=revision, capability="0" * 64)
        self.assertEqual(nothing_created.exception.code, "drain_not_held",
                         "no hold was created by a malformed requestId")

    def test_a_redirect_answer_is_never_followed_with_the_token(self):
        """The relay's answer is final: no 3xx is followed, so the bearer token
        (and the drain capability) can never be forwarded to another origin."""
        stolen = []

        def canary(request):
            stolen.append((request.path, request.headers.get("authorization")))
            send_json(request, 200, {"quiesced": True})

        canary_server, canary_port = start_http_server(canary)
        target = "http://127.0.0.1:%d/v1/admin/update/drain" % canary_port

        def redirect(request):
            request.send_response(302)
            request.send_header("location", target)
            request.send_header("content-length", "0")
            request.end_headers()

        redirect_server, redirect_port = start_http_server(redirect)
        base = "http://127.0.0.1:%d" % redirect_port
        try:
            for verb, call in (
                ("acquire", lambda: update_drain.acquire_drain(
                    base, "txn-r", token=TOKEN, wait_s=0.0)),
                ("status", lambda: update_drain.drain_status(
                    base, "txn-r", token=TOKEN, revision="a" * 40,
                    capability="0" * 64)),
                ("release", lambda: update_drain.release_drain(
                    base, "txn-r", token=TOKEN, revision="a" * 40,
                    capability="0" * 64))):
                with self.subTest(verb=verb):
                    with self.assertRaises(update_drain.DrainError) as refused:
                        call()
                    self.assertEqual(refused.exception.code, "drain_unreachable")
        finally:
            stop_http_server(redirect_server)
            stop_http_server(canary_server)
        self.assertEqual(stolen, [], "no request, token-bearing or not, reached the "
                                    "redirect target")

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
        # A finished task to name as a parent below: `parentTaskId` is the one
        # caller-settable field that looks like the engine's internal path.
        parent_task = relay.run_task(agentId="slow", input="0.01")
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
        # to opt out. A valid task naming a parent — the request field that
        # most resembles the engine's own child creation — is refused by the
        # same drain gate as every other new admission, and the very same body
        # is admitted once the hold ends, so the refusal is the drain's.
        delegated = {"agentId": "echo", "input": "bang",
                     "parentTaskId": parent_task["id"]}
        status, refusal = relay.submit(**delegated)
        self.assertEqual((status, refusal.get("error")), (503, "drain_active"),
                         "a request field never bypasses the drain")
        self.assertEqual(relay.submit(agentId="echo", input="external")[0], 503)
        update_drain.release_drain(relay.base, "txn-wf", token=TOKEN,
                                   revision=acquired["revision"],
                                   capability=acquired["capability"])
        self.assertEqual(relay.submit(**delegated)[0], 202,
                         "the same body is admitted as soon as the drain is released")

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

    # --------------------------------------------------- persistent barrier

    def test_post_update_state_refuses_a_barrier_and_reopens_admission_after_it(self):
        """POST /v1/admin/update/state is safe_update._refresh_barrier's real
        route: it re-reads the private barrier from disk, so a barrier that a
        finished activation removed on disk reopens admission on the live
        relay, and a barrier that boots with the relay holds admission closed
        through the very same endpoint."""
        identity.ensure_device_id(Path(self.state))
        # Learn the commit the barrier must bind from a relay without a barrier.
        revision = update_drain._server_revision(self.start([self.sleeper()]).base)
        self.relays[0].close()
        self.relays.clear()
        update_transaction._write_barrier(Path(self.state), {
            "schema": update_transaction.BARRIER_SCHEMA,
            "transactionId": "txn-barrier-test",
            "phase": "prepared",
            "revision": revision,
            "binding": update_transaction.install_fingerprint(Path(self.state))})
        # A relay that boots inside the barrier must refuse admission.
        relay = self.start([self.sleeper()],
                           env={"AGENT_RELAY_STATE_DIR": self.state})
        status, view = relay.request("POST", "/v1/admin/update/state")
        self.assertEqual(status, 200)
        self.assertTrue(view["admissionClosed"])
        self.assertEqual(view["barrier"]["transactionId"], "txn-barrier-test")
        self.assertEqual(relay.submit(agentId="slow", input="0.05")[0], 503)
        # The activation journal finished and removed its file from disk; the
        # real _refresh_barrier POST must make the live relay re-read that.
        os.unlink(Path(self.state) / "update" / "admission-barrier.json")
        refresh = safe_update._refresh_barrier(relay.base, TOKEN)
        self.assertFalse(refresh.get("admissionClosed"))
        self.assertIsNone(refresh.get("barrier"))
        self.assertEqual(relay.submit(agentId="slow", input="0.05")[0], 202,
                         "admission reopens on the live relay after the barrier "
                         "journal is finished")

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
                              self._fake_stage_success), \
                    patch.object(safe_update, "_consumed_consent", self._fake_consent), \
                    patch.dict(os.environ, {"AGENT_RELAY_TOKEN": TOKEN}):
                self.assertEqual(
                    safe_update.cmd_drain("0.4.0", os.getcwd(), base_url=relay.base,
                                          transaction_id="cli-slow", quiesce_wait_s=0.5), 1)
            release_adapter.set()
            # Even a rehearsal that never quiesces releases: the hold never
            # spills out of it.
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

    # ---------------------------------------------------- real stage and consent

    def _real_stage(self, fixture, state, now):
        """Stage the fixture's release for real: real git, real receipt, and
        the consent record the stage burns on its way through."""
        fixture.approve(state)
        home = Path(fixture.tmp.name)
        git = update_staging.Git(str(staging_fixture._fake_git_script(
            home, home / "fake-git.log", str(fixture.origin))))
        return update_staging.stage_release(
            fixture.identity(), root=fixture.repo, state_dir=state, git=git,
            manifest=fixture.manifest, manifest_bytes=fixture.manifest_bytes, now=now)

    def test_drain_cli_verifies_a_real_stage_receipt_and_consumed_consent(self):
        """The CLI's own precondition, unpatched: the receipt on disk and the
        consumed consent decide it, and a tampered one is refused before any
        hold is taken."""
        with staging_fixture._Fixture() as fixture:
            home = Path(fixture.tmp.name)
            state = home / "agent-relay"
            self._real_stage(fixture, state, staging_fixture.NOW + 10)
            relay = self.start([self.sleeper()])
            environ = {"AGENT_RELAY_TOKEN": TOKEN, "XDG_STATE_HOME": str(home)}
            with patch.dict(os.environ, environ):
                self.assertEqual(
                    safe_update.cmd_drain(staging_fixture.TAG, str(fixture.repo),
                                          base_url=relay.base, transaction_id="real-txn",
                                          quiesce_wait_s=10), 0)
                self.assertEqual(relay.submit(agentId="slow", input="0.05")[0], 202)
                # The real receipt is what makes a drain legal: a different
                # install dir has no stage staged from it.
                with self.assertRaises(safe_update.UpdateError) as foreign:
                    safe_update._stage_receipt(staging_fixture.TAG,
                                               str(home / "other-checkout"), state)
                self.assertIn("no verified stage", str(foreign.exception))
                # And the real consent record must show staging consumed it.
                consent_path = state / "consent.json"
                consent = json.loads(consent_path.read_text())
                self.assertIs(consent["consumed"], True)
                consent["consumed"] = False
                consent_path.write_text(json.dumps(consent, indent=1))
                with self.assertRaises(safe_update.UpdateError) as unconsumed:
                    safe_update._consumed_consent(fixture.identity(), state)
                self.assertIn("never consumed", str(unconsumed.exception))
                # An unconsumed consent refuses the whole command before the
                # relay is touched, so admission never closes.
                with self.assertRaises(safe_update.UpdateError):
                    safe_update.cmd_drain(staging_fixture.TAG, str(fixture.repo),
                                          base_url=relay.base, transaction_id="nope",
                                          quiesce_wait_s=1)
            self.assertEqual(relay.submit(agentId="slow", input="0.05")[0], 202,
                             "a refused precondition takes no hold")

    def test_newest_stage_is_chosen_by_receipt_time_not_directory_name(self):
        """Two real stages of one tag: the receipts' createdAt decides, even
        when the directory names sort the other way."""
        with staging_fixture._Fixture() as fixture:
            home = Path(fixture.tmp.name)
            state = home / "agent-relay"
            older = self._real_stage(fixture, state, staging_fixture.NOW + 10)
            newer = self._real_stage(fixture, state, staging_fixture.NOW + 20)
            self.assertLess(older["createdAt"], newer["createdAt"])
            # stage-<tag>-<random> names carry no age; make the older one sort
            # last so name order and receipt time disagree.
            moved = Path(older["stage"]).with_name(Path(newer["stage"]).name + "-zz")
            os.rename(older["stage"], moved)
            receipt_path = moved / update_staging.STAGE_RECEIPT_NAME
            body = json.loads(receipt_path.read_text())
            body["stage"] = str(moved)
            receipt_path.write_text(json.dumps(body, indent=1))
            chosen, stored = safe_update._stage_receipt(
                staging_fixture.TAG, str(fixture.repo), state)
            self.assertEqual(chosen["createdAt"], newer["createdAt"],
                             "the newest succeeded stage wins, not the last name")
            self.assertEqual(chosen["stage"], newer["stage"])
            self.assertEqual(stored, fixture.identity())

    def test_drain_cli_refuses_a_failed_stage_before_acquiring(self):
        """The stage check failing is not a rehearsal: no hold is taken, so
        nothing needs releasing and admission is never closed."""
        relay = self.start([self.sleeper()])
        called = []
        real_request = update_drain._request

        def recording(method, base_url, path, **kwargs):
            called.append(path)
            return real_request(method, base_url, path, **kwargs)

        with patch.object(safe_update, "_stage_receipt", self._stage_failure), \
                patch.object(safe_update, "_consumed_consent", self._fake_consent), \
                patch.object(update_drain, "_request", recording), \
                patch.dict(os.environ, {"AGENT_RELAY_TOKEN": TOKEN}):
            with self.assertRaises(safe_update.UpdateError) as refused:
                safe_update.cmd_drain("0.4.0", os.getcwd(), base_url=relay.base,
                                      transaction_id="cli-bad", quiesce_wait_s=1)
        self.assertIn("no verified stage of 0.4.0", str(refused.exception))
        self.assertEqual(called, [], "the stage is proven before any admin verb")
        self.assertEqual(relay.submit(agentId="slow", input="0.05")[0], 202,
                         "a failed stage never closed admission")

    # The fake seams above exist only to reach the timeout and mid-rehearsal
    # paths; the real receipt and consent verification has its own test
    # (test_drain_cli_verifies_a_real_stage_receipt_and_consumed_consent).
    def _fake_stage_success(self, tag, install_dir, state_dir):
        return {}, {"tag": tag}

    def _stage_failure(self, tag, install_dir, state_dir):
        raise safe_update.UpdateError("no verified stage of %s staged from %s"
                                      % (tag, install_dir))

    def _fake_consent(self, identity, state_dir):
        return {}


if __name__ == "__main__":
    unittest.main()
