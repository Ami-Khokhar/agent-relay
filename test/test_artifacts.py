"""Artifact service tests: publish, versions, comments, sessions, mention routing."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib import error as urlerror
from urllib.parse import urlsplit
from urllib import request as urlrequest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = sys.executable
SERVER = os.path.join(ROOT, "src", "artifact_server.py")
CLI = os.path.join(ROOT, "src", "artifact_cli.py")
# Set these to run the same suite against the Rust build:
#   cargo build --release --manifest-path rust/Cargo.toml
#   ARTIFACT_SERVER_BIN=rust/target/release/artifactd \
#   ARTIFACT_CLI_BIN=rust/target/release/artifactd python3 -m pytest test/test_artifacts.py
SERVER_BIN = os.environ.get("ARTIFACT_SERVER_BIN")
CLI_BIN = os.environ.get("ARTIFACT_CLI_BIN")


def server_command():
    return [SERVER_BIN, "serve"] if SERVER_BIN else [PYTHON, SERVER]


def cli_command(*args):
    return [CLI_BIN, *args] if CLI_BIN else [PYTHON, CLI, *args]

SESSION_VARS = ("AGENT_ARTIFACTS_SESSION", "PI_SESSION_ID", "CLAUDE_SESSION_ID",
                "CODEX_SESSION_ID", "OPENCODE_SESSION_ID")

PAGE = """<!doctype html>
<html><head><title>Demo</title></head>
<body><h1 id="headline">Quarterly review</h1>
<p class="lead">Numbers look flat.</p></body></html>
"""


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Artifacts:
    """Runs src/artifact_server.py against a throwaway state directory."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="agent-artifacts-")
        self.port = free_port()
        self.content_port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.content = f"http://127.0.0.1:{self.content_port}"
        env = {**os.environ, "AGENT_ARTIFACTS_PORT": str(self.port),
               "AGENT_ARTIFACTS_CONTENT_PORT": str(self.content_port),
               "AGENT_ARTIFACTS_DIR": self.dir, "AGENT_ARTIFACTS_LEASE_SECONDS": "20"}
        for name in SESSION_VARS:
            env.pop(name, None)
        self.proc = subprocess.Popen(server_command(), cwd=ROOT, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.time() + 8
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(self.proc.stderr.read().decode())
            try:
                with urlrequest.urlopen(self.base + "/healthz", timeout=0.4) as response:
                    if response.status == 200:
                        return
            except Exception:  # noqa: BLE001
                time.sleep(0.05)
        raise RuntimeError("artifact server did not start")

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def json(self, method, path, payload=None, timeout=15):
        data = None if payload is None else json.dumps(payload).encode()
        request = urlrequest.Request(self.base + path, data=data, method=method)
        request.add_header("x-artifacts-client", "1")
        if data is not None:
            request.add_header("content-type", "application/json")
        with urlrequest.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode() or "{}")

    def text(self, path, timeout=15, content=False):
        root = self.content if content else self.base
        with urlrequest.urlopen(root + path, timeout=timeout) as response:
            return response.status, response.read().decode()

    def cli(self, *args, timeout=40):
        env = {**os.environ, "AGENT_ARTIFACTS_URL": self.base, "AGENT_ARTIFACTS_DIR": self.dir}
        for name in SESSION_VARS:
            env.pop(name, None)
        result = subprocess.run(cli_command(*args), cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise AssertionError(f"cli {args} failed: {result.stderr}")
        return result.stdout


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.service = Artifacts()

    def tearDown(self):
        self.service.stop()

    def publish(self, html=PAGE, **extra):
        payload = {"html": html, "title": "Quarterly review", **extra}
        status, body = self.service.json("POST", "/api/artifacts", payload)
        self.assertEqual(status, 201, body)
        return body

    def comment(self, slug, body, **extra):
        status, payload = self.service.json("POST", f"/api/artifacts/{slug}/comments",
                                            {"body": body, "author": "owner", **extra})
        self.assertEqual(status, 201, payload)
        return payload

    # ---------------------------------------------------------------- basics

    def test_publishes_serves_and_versions_one_artifact(self):
        first = self.publish(**{"as": "@pi-deepseek"})
        self.assertEqual(first["version"], 1)
        self.assertTrue(first["slug"].startswith("quarterly-review"))
        self.assertEqual(first["origin"]["handle"], "@pi-deepseek")
        status, html = self.service.text(f"/a/{first['slug']}/raw", content=True)
        self.assertEqual(status, 200)
        self.assertIn("Quarterly review", html)
        self.assertNotIn("__ARTIFACTS", html)

        second = self.publish(note="wording pass", slug=first["slug"], **{"as": "@pi-deepseek"})
        self.assertEqual(second["version"], 2)
        _, artifact = self.service.json("GET", f"/api/artifacts/{first['slug']}")
        self.assertEqual([item["n"] for item in artifact["versions"]], [1, 2])
        self.assertEqual(artifact["versions"][1]["note"], "wording pass")
        self.assertEqual(artifact["versions"][1]["by"], "@pi-deepseek")
        _, v1 = self.service.text(f"/a/{first['slug']}/raw?v=1", content=True)
        self.assertIn("Numbers look flat", v1)

    def test_review_mode_injects_the_overlay_only(self):
        artifact = self.publish()
        _, reviewed = self.service.text(f"/a/{artifact['slug']}/raw?review=1&v=1", content=True)
        self.assertIn('data-slug="', reviewed)
        self.assertIn('data-parent="', reviewed)
        self.assertNotIn("window.__ARTIFACTS", reviewed, "no inline script: the CSP blocks it")
        self.assertIn("/_artifacts/overlay.js", reviewed)
        _, overlay = self.service.text("/_artifacts/overlay.js", content=True)
        self.assertIn("postMessage", overlay)
        status, view = self.service.text(f"/a/{artifact['slug']}")
        self.assertEqual(status, 200)
        self.assertIn("Comment mode", view)

    # --------------------------------------------------------------- sessions

    def test_publish_attaches_the_origin_session_and_reattach_is_idempotent(self):
        artifact = self.publish(**{"as": "@pi-deepseek"})
        slug = artifact["slug"]
        _, sessions = self.service.json("GET", f"/api/artifacts/{slug}/sessions")
        self.assertEqual(len(sessions["sessions"]), 1)
        record = sessions["sessions"][0]
        self.assertEqual(record["handle"], "@pi-deepseek")
        self.assertEqual(record["role"], "origin")
        self.assertFalse(record["live"])
        self.assertEqual(sessions["origin"]["session"], record["id"])

        again = self.service.json("POST", f"/api/artifacts/{slug}/sessions",
                                  {"as": "@pi-deepseek"})[1]
        self.assertEqual(again["session"]["id"], record["id"], "same handle must reuse its session")
        self.assertEqual(len(again["sessions"]), 1)

        other = self.service.json("POST", f"/api/artifacts/{slug}/sessions",
                                  {"as": "@codex", "session": "s-codex-1", "cwd": "/tmp/app"})[1]
        self.assertEqual(len(other["sessions"]), 2)
        self.assertEqual(other["session"]["cwd"], "/tmp/app")

    def test_a_new_session_can_join_an_artifact_after_the_first_one_closed(self):
        artifact = self.publish(**{"as": "@pi-deepseek"})
        slug = artifact["slug"]
        first = self.service.json("GET", f"/api/artifacts/{slug}/sessions")[1]["sessions"][0]
        detached = self.service.json("DELETE", f"/api/artifacts/{slug}/sessions/{first['id']}")[1]
        self.assertEqual(detached["sessions"], [])

        joined = self.service.json("POST", f"/api/artifacts/{slug}/sessions",
                                   {"as": "@pi-deepseek", "session": "s-new-1"})[1]
        self.assertEqual(joined["session"]["id"], "s-new-1")
        self.assertEqual(len(joined["sessions"]), 1)

    def test_presence_tracks_only_active_waiters(self):
        artifact = self.publish(**{"as": "@codex"})
        slug = artifact["slug"]

        def waiter():
            self.service.json("POST", f"/api/artifacts/{slug}/inbox",
                              {"as": "@codex", "wait": 6}, timeout=20)

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.8)
        _, sessions = self.service.json("GET", f"/api/artifacts/{slug}/sessions")
        self.assertTrue(sessions["sessions"][0]["live"])
        self.assertGreater(sessions["sessions"][0]["leaseLeft"], 0)
        thread.join(timeout=15)
        _, after = self.service.json("GET", f"/api/artifacts/{slug}/sessions")
        self.assertTrue(after["sessions"][0]["live"], "the last lease outlives the poll")

    # ---------------------------------------------------------------- routing

    def test_mentions_route_to_one_agent_and_only_that_agent_claims(self):
        artifact = self.publish(**{"as": "@pi-deepseek"})
        slug = artifact["slug"]
        self.service.json("POST", f"/api/artifacts/{slug}/sessions", {"as": "@codex"})

        comment = self.comment(slug, "@codex split the table out of the summary")
        self.assertEqual(comment["to"], ["@codex"], "the mention decides the routing, not the author")

        _, mine = self.service.json("POST", f"/api/artifacts/{slug}/inbox", {"as": "@pi-deepseek"})
        self.assertEqual(mine["comments"], [])
        _, theirs = self.service.json("POST", f"/api/artifacts/{slug}/inbox", {"as": "@codex"})
        self.assertEqual([item["id"] for item in theirs["comments"]], [comment["id"]])
        self.assertEqual(theirs["comments"][0]["delivery"][0]["state"], "claimed")
        self.assertEqual(theirs["comments"][0]["delivery"][0]["handle"], "@codex")

        _, again = self.service.json("POST", f"/api/artifacts/{slug}/inbox", {"as": "@codex"})
        self.assertEqual(again["comments"], [], "a claimed comment is not handed out twice")

    def test_a_subresource_get_cannot_claim_a_comment(self):
        artifact = self.publish(**{"as": "@pi-deepseek"})
        slug = artifact["slug"]
        comment = self.comment(slug, "@pi-deepseek tighten it")
        # What an <img src> or <iframe src> inside an artifact can do: a plain GET.
        with self.assertRaises(urlerror.HTTPError) as caught:
            self.service.text(f"/api/artifacts/{slug}/inbox?as=@pi-deepseek")
        self.assertEqual(caught.exception.code, 405)
        _, listing = self.service.json("GET", f"/api/artifacts/{slug}/comments")
        self.assertEqual(listing["comments"][0]["claims"], {}, "nothing may be claimed by a GET")
        _, claimed = self.service.json("POST", f"/api/artifacts/{slug}/inbox",
                                       {"as": "@pi-deepseek"})
        self.assertEqual([item["id"] for item in claimed["comments"]], [comment["id"]])

    def test_a_subresource_get_cannot_claim_through_the_comments_route(self):
        artifact = self.publish(**{"as": "@pi-deepseek"})
        slug = artifact["slug"]
        self.comment(slug, "@pi-deepseek this is yours")
        # The exact replay: a GET with a handle must read, never claim.
        status, listing = self.service.json(
            "GET", f"/api/artifacts/{slug}/comments?as=@pi-deepseek&wait=1")
        self.assertEqual(status, 200)
        self.assertEqual(listing["comments"][0]["claims"], {})
        _, again = self.service.json("POST", f"/api/artifacts/{slug}/inbox",
                                     {"as": "@pi-deepseek"})
        self.assertEqual(len(again["comments"]), 1, "the comment is still claimable")

    def test_state_changes_need_the_client_header(self):
        artifact = self.publish(**{"as": "@pi-deepseek"})
        slug = artifact["slug"]
        payload = json.dumps({"body": "from a cross-site form"}).encode()
        for method, path in (("POST", f"/api/artifacts/{slug}/comments"),
                             ("POST", f"/api/artifacts/{slug}/inbox"),
                             ("POST", "/api/artifacts"),
                             ("DELETE", f"/api/artifacts/{slug}")):
            request = urlrequest.Request(self.service.base + path, data=payload, method=method)
            request.add_header("content-type", "application/json")
            with self.assertRaises(urlerror.HTTPError) as caught:
                urlrequest.urlopen(request, timeout=10)
            self.assertEqual(caught.exception.code, 400, f"{method} {path}")

    def test_unaddressed_comment_still_routes_to_the_origin_after_it_detaches(self):
        artifact = self.publish(**{"as": "@pi-deepseek"})
        slug = artifact["slug"]
        origin = self.service.json("GET", f"/api/artifacts/{slug}/sessions")[1]["sessions"][0]
        self.service.json("DELETE", f"/api/artifacts/{slug}/sessions/{origin['id']}")
        comment = self.comment(slug, "no mention, and the publisher is gone")
        self.assertEqual(comment["to"], ["@pi-deepseek"], "routing survives a detached origin")
        self.assertEqual(comment["delivery"][0]["state"], "waiting")
        status, page = self.service.text(f"/a/{slug}/raw?v=1", content=True)
        self.assertEqual(status, 200)

    def test_artifacts_live_on_their_own_origin(self):
        artifact = self.publish(**{"as": "@codex"})
        # The API origin must not serve artifact HTML at all.
        with self.assertRaises(urlerror.HTTPError) as caught:
            self.service.text(f"/a/{artifact['slug']}/raw?v=1")
        self.assertEqual(caught.exception.code, 404)

        request = urlrequest.Request(f"{self.service.content}/a/{artifact['slug']}/raw?v=1")
        with urlrequest.urlopen(request, timeout=10) as response:
            policy = response.headers.get("content-security-policy", "")
            self.assertEqual(response.status, 200)
        self.assertIn("connect-src 'none'", policy, policy)
        self.assertIn(f"frame-ancestors {self.service.base}", policy, policy)
        self.assertIn("script-src 'self'", policy, policy)

    def test_artifact_origin_is_not_the_api_origin(self):
        # Same-origin artifact HTML could reach the API through window.parent;
        # a different origin is what makes that impossible.
        artifact = self.publish(**{"as": "@codex"})
        _, api_body = self.service.json("GET", "/healthz")
        self.assertIn("artifacts", api_body, "the API origin answers JSON health")
        status, content_body = self.service.text("/healthz", content=True)
        self.assertEqual((status, content_body), (200, "ok"), "the content origin is not the API")
        _, summary = self.service.json("GET", f"/api/artifacts/{artifact['slug']}")
        self.assertNotEqual(urlsplit(summary["rawBase"]).port, urlsplit(self.service.base).port)

    def test_content_port_answers_bad_input_instead_of_dropping(self):
        artifact = self.publish(**{"as": "@codex"})
        for query, expected in (("?v=abc", 400), ("?v=999", 404)):
            request = f"{self.service.content}/a/{artifact['slug']}/raw{query}"
            with self.assertRaises(urlerror.HTTPError) as caught:
                urlrequest.urlopen(request, timeout=10)
            self.assertEqual(caught.exception.code, expected, query)
            self.assertIn("error", json.loads(caught.exception.read().decode()))
        with self.assertRaises(urlerror.HTTPError) as caught:
            urlrequest.urlopen(f"{self.service.content}/nope", timeout=10)
        self.assertEqual(caught.exception.code, 404)

    def test_review_page_frames_the_content_origin(self):
        artifact = self.publish(**{"as": "@codex"})
        _, summary = self.service.json("GET", f"/api/artifacts/{artifact['slug']}")
        self.assertTrue(summary["rawBase"].startswith(self.service.content), summary["rawBase"])

    def test_a_mention_to_an_absent_agent_waits_in_the_queue(self):
        artifact = self.publish(**{"as": "@pi-deepseek"})
        slug = artifact["slug"]
        comment = self.comment(slug, "@codex this is yours when you are back")
        _, listing = self.service.json("GET", f"/api/artifacts/{slug}/comments")
        state = listing["comments"][0]["delivery"][0]
        self.assertEqual(state["handle"], "@codex")
        self.assertEqual(state["state"], "waiting", "absent agent means queued, not lost")
        self.assertFalse(state["live"])

        time.sleep(0.2)
        self.service.json("POST", f"/api/artifacts/{slug}/sessions", {"as": "@codex"})
        _, claimed = self.service.json("POST", f"/api/artifacts/{slug}/inbox", {"as": "@codex"})
        self.assertEqual([item["id"] for item in claimed["comments"]], [comment["id"]])

    def test_multiple_mentions_reach_every_agent_named(self):
        artifact = self.publish(**{"as": "@pi-deepseek"})
        slug = artifact["slug"]
        self.service.json("POST", f"/api/artifacts/{slug}/sessions", {"as": "@codex"})
        comment = self.comment(slug, "@codex and @pi-deepseek both need this")
        self.assertEqual(comment["to"], ["@codex", "@pi-deepseek"])
        _, pi = self.service.json("POST", f"/api/artifacts/{slug}/inbox", {"as": "@pi-deepseek"})
        _, codex = self.service.json("POST", f"/api/artifacts/{slug}/inbox", {"as": "@codex"})
        self.assertEqual(len(pi["comments"]), 1)
        self.assertEqual(len(codex["comments"]), 1)

    def test_an_unaddressed_comment_goes_to_the_agent_that_published(self):
        artifact = self.publish(**{"as": "@pi-deepseek"})
        slug = artifact["slug"]
        self.service.json("POST", f"/api/artifacts/{slug}/sessions", {"as": "@codex"})
        comment = self.comment(slug, "no mention here")
        self.assertEqual(comment["to"], ["@pi-deepseek"], "default target is the origin agent")

        _, other = self.service.json("POST", f"/api/artifacts/{slug}/inbox", {"as": "@codex"})
        self.assertEqual(other["comments"], [])
        _, pinged = self.service.json("POST", f"/api/artifacts/{slug}/inbox", {"as": "@pi-deepseek"})
        self.assertEqual([item["id"] for item in pinged["comments"]], [comment["id"]])

        _, listing = self.service.json("GET", f"/api/artifacts/{slug}/comments")
        self.assertEqual(listing["comments"][0]["delivery"][0]["handle"], "@pi-deepseek")
        self.assertEqual(listing["comments"][0]["delivery"][0]["state"], "claimed")

    def test_inbox_all_takes_comments_addressed_to_others(self):
        artifact = self.publish(**{"as": "@pi-deepseek"})
        slug = artifact["slug"]
        self.service.json("POST", f"/api/artifacts/{slug}/sessions", {"as": "@codex"})
        self.comment(slug, "@codex only")
        _, normal = self.service.json("POST", f"/api/artifacts/{slug}/inbox", {"as": "@pi-deepseek"})
        _, everything = self.service.json(
            "POST", f"/api/artifacts/{slug}/inbox", {"as": "@pi-deepseek", "all": True})
        self.assertEqual(normal["comments"], [])
        self.assertEqual(len(everything["comments"]), 1)

    def test_release_puts_a_claimed_comment_back_in_the_queue(self):
        artifact = self.publish(**{"as": "@pi-deepseek"})
        slug = artifact["slug"]
        comment = self.comment(slug, "@pi-deepseek tighten the lede")
        _, first = self.service.json("POST", f"/api/artifacts/{slug}/inbox", {"as": "@pi-deepseek"})
        self.assertEqual(len(first["comments"]), 1)
        _, empty = self.service.json("POST", f"/api/artifacts/{slug}/inbox", {"as": "@pi-deepseek"})
        self.assertEqual(empty["comments"], [])

        status, released = self.service.json("POST", f"/api/comments/{comment['id']}/release",
                                             {"author": "@pi-deepseek"})
        self.assertEqual(status, 200)
        self.assertEqual(released["delivery"][0]["state"], "queued")
        _, again = self.service.json("POST", f"/api/artifacts/{slug}/inbox", {"as": "@pi-deepseek"})
        self.assertEqual([item["id"] for item in again["comments"]], [comment["id"]])

    def test_inbox_long_poll_returns_the_moment_a_comment_lands(self):
        artifact = self.publish(**{"as": "@pi-deepseek"})
        slug = artifact["slug"]
        result = {}

        def waiter():
            started = time.time()
            status, body = self.service.json(
                "POST", f"/api/artifacts/{slug}/inbox",
                {"as": "@pi-deepseek", "wait": 20}, timeout=30)
            result["elapsed"] = time.time() - started
            result["body"] = body

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.7)
        self.comment(slug, "@pi-deepseek the headline is vague")
        thread.join(timeout=25)
        self.assertEqual(len(result["body"]["comments"]), 1)
        self.assertGreater(result["elapsed"], 0.4)

    # --------------------------------------------------------------- threads

    def test_comments_are_anchored_by_version_and_survive_a_new_version(self):
        artifact = self.publish(**{"as": "@pi-deepseek"})
        anchor = {"selector": "#headline", "quote": "Quarterly review", "x": 0.5, "y": 0.5}
        comment = self.comment(artifact["slug"], "Lead with the number.",
                               version=1, anchor=anchor)
        self.assertEqual(comment["anchor"]["selector"], "#headline")
        self.assertEqual(comment["seq"], 1)

        self.publish(slug=artifact["slug"], note="second pass", **{"as": "@pi-deepseek"})
        _, listing = self.service.json("GET", f"/api/artifacts/{artifact['slug']}/comments")
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["comments"][0]["version"], 1)

    def test_replies_resolution_and_open_filter(self):
        artifact = self.publish(**{"as": "@pi-deepseek"})
        slug = artifact["slug"]
        comment = self.comment(slug, "Tighten the headline")
        status, reply = self.service.json(
            "POST", f"/api/comments/{comment['id']}/reply",
            {"body": "Rewritten to 6 words.", "author": "@pi-deepseek"})
        self.assertEqual(status, 200)
        self.assertEqual(reply["author"], "@pi-deepseek")

        _, resolved = self.service.json("POST", f"/api/comments/{comment['id']}/status",
                                        {"status": "resolved", "author": "owner"})
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["delivery"][0]["state"], "resolved")
        _, open_only = self.service.json("GET", f"/api/artifacts/{slug}/comments?open=1")
        self.assertEqual(open_only["comments"], [])
        _, everything = self.service.json("GET", f"/api/artifacts/{slug}/comments")
        self.assertEqual(everything["comments"][0]["replies"][0]["body"], "Rewritten to 6 words.")

    def test_two_servers_on_one_state_directory_keep_every_comment(self):
        """Two processes, one state directory: no comment may be lost."""
        artifact = self.publish(**{"as": "@codex"})
        slug = artifact["slug"]
        second_port, second_content = free_port(), free_port()
        env = {**os.environ, "AGENT_ARTIFACTS_PORT": str(second_port),
               "AGENT_ARTIFACTS_CONTENT_PORT": str(second_content),
               "AGENT_ARTIFACTS_DIR": self.service.dir}
        for name in SESSION_VARS:
            env.pop(name, None)
        other = subprocess.Popen(server_command(), cwd=ROOT, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second_base = f"http://127.0.0.1:{second_port}"
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                with urlrequest.urlopen(second_base + "/healthz", timeout=0.4) as response:
                    if response.status == 200:
                        break
            except Exception:  # noqa: BLE001
                time.sleep(0.05)
        else:
            other.kill()
            self.fail("the second server did not start")

        def post(index, base):
            data = json.dumps({"body": f"racer {index}", "author": "@owner"}).encode()
            request = urlrequest.Request(f"{base}/api/artifacts/{slug}/comments",
                                         data=data, method="POST")
            request.add_header("content-type", "application/json")
            request.add_header("x-artifacts-client", "1")
            with urlrequest.urlopen(request, timeout=20) as response:
                return response.status

        try:
            total = 12
            threads = [threading.Thread(
                target=lambda i=index: results.append(
                    post(i, self.service.base if i % 2 == 0 else second_base)))
                for index in range(total)]
            results = []
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=25)
            self.assertEqual(results.count(201), total, results)
            _, listing = self.service.json("GET", f"/api/artifacts/{slug}/comments")
            self.assertEqual(listing["count"], total, "every acknowledged comment is stored")
        finally:
            other.terminate()
            try:
                other.wait(timeout=5)
            except subprocess.TimeoutExpired:
                other.kill()

    def test_index_healthz_and_delete(self):
        artifact = self.publish(**{"as": "@pi-deepseek"})
        status, page = self.service.text("/")
        self.assertEqual(status, 200)
        self.assertIn(artifact["slug"], page)
        self.assertIn("@pi-deepseek", page)
        _, health = self.service.json("GET", "/healthz")
        self.assertEqual(health["artifacts"], 1)
        self.assertEqual(self.service.json("DELETE", f"/api/artifacts/{artifact['slug']}")[0], 200)
        _, listing = self.service.json("GET", "/api/artifacts")
        self.assertEqual(listing["artifacts"], [])
        with self.assertRaises(urlerror.HTTPError) as caught:
            self.service.text(f"/a/{artifact['slug']}")
        self.assertEqual(caught.exception.code, 404)

    def test_rejects_bad_input(self):
        with self.assertRaises(urlerror.HTTPError) as caught:
            self.service.json("POST", "/api/artifacts", {"html": ""})
        self.assertEqual(caught.exception.code, 400)
        artifact = self.publish(**{"as": "@pi-deepseek"})
        with self.assertRaises(urlerror.HTTPError) as caught:
            self.service.json("POST", f"/api/artifacts/{artifact['slug']}/comments", {"body": "  "})
        self.assertEqual(caught.exception.code, 400)
        with self.assertRaises(urlerror.HTTPError) as caught:
            self.service.json("POST", f"/api/artifacts/{artifact['slug']}/sessions",
                              {"as": "not a handle"})
        self.assertEqual(caught.exception.code, 400)
        with self.assertRaises(urlerror.HTTPError) as caught:
            self.service.json("POST", "/api/artifacts/missing/inbox", {"as": "@pi"})
        self.assertEqual(caught.exception.code, 404)

    # -------------------------------------------------------------------- CLI

    def test_cli_whoami_publish_attach_sessions_and_inbox(self):
        page = os.path.join(self.service.dir, "page.html")
        with open(page, "w", encoding="utf-8") as handle:
            handle.write(PAGE)

        env_handle = {"AGENT_ARTIFACTS_AS": "@pi-deepseek"}
        published = json.loads(self.service.cli("publish", page, "--title", "CLI check",
                                                "--as", "@pi-deepseek", "--json"))
        self.assertEqual(published["version"], 1)
        slug = published["slug"]

        # A second agent joins the same artifact with its own handle.
        env = {**os.environ, "AGENT_ARTIFACTS_URL": self.service.base,
               "AGENT_ARTIFACTS_DIR": self.service.dir, **env_handle}
        for name in SESSION_VARS:
            env.pop(name, None)
        env.update(env_handle)
        attached = subprocess.run([PYTHON, CLI, "attach", slug, "--as", "@codex", "--json"],
                                  cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(attached.returncode, 0, attached.stderr)
        self.assertEqual(json.loads(attached.stdout)["session"]["handle"], "@codex")
        sessions = json.loads(self.service.cli("sessions", slug, "--json"))
        self.assertEqual([item["handle"] for item in sessions["sessions"]],
                         ["@pi-deepseek", "@codex"])

        self.comment(slug, "@codex take the table, @pi-deepseek take the summary")
        result = {}

        def waiter():
            started = time.time()
            result["elapsed"] = None
            result["payload"] = json.loads(self.service.cli(
                "inbox", slug, "--as", "@codex", "--wait", "20", "--json"))
            result["elapsed"] = time.time() - started

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.6)
        self.comment(slug, "@codex second one")
        thread.join(timeout=30)
        self.assertTrue(result["payload"]["comments"], "inbox should find the queued comment")
        self.assertEqual(result["payload"]["handle"], "@codex")

        self.service.cli("reply", result["payload"]["comments"][0]["id"], "On it.", "--as", "@codex")
        _, listing = self.service.json("GET", f"/api/artifacts/{slug}/comments")
        authors = [reply["author"] for comment in listing["comments"] for reply in comment["replies"]]
        self.assertIn("@codex", authors)

        mapping = json.loads(self.service.cli("map", "--json"))
        self.assertEqual(mapping["artifacts"][0]["handles"], ["@pi-deepseek", "@codex"])
        whoami = json.loads(self.service.cli("whoami", "--json"))
        self.assertTrue(whoami["handle"].startswith("@"))


if __name__ == "__main__":
    unittest.main()
