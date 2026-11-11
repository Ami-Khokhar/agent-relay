"""Integrated acceptance of the safe updater over the real public CLI, the real
relay HTTP server and the real workflow engine (#119, #160).

Every scenario drives the same commands ``main()`` dispatches — ``inspect``,
``approve``, ``stage``, ``drain``, ``prepare-activation``, ``activate``,
``recover`` and ``recover-abort`` — inside temporary HOME, state and install
trees. The updater logic, the relay HTTP server (``src/server.py``) and the
workflow engine all run for real; the only fakes are the canonical GitHub HTTPS
transport (release metadata is answered in-process), the git process boundary
(a forwarding script translates the canonical clone URL to a local fixture
origin, so nothing is ever fetched from the network), the interactive TTY, and
the per-user service manager: the real ``inspect_service`` validates the unit,
and only the restart is faked by stopping the running relay process and
spawning a fresh one from the same install tree on the same port — exactly
what the per-user manager verbs would produce, without touching a host service.

This is implementation verification on isolated synthetic boundaries. No
release is published, nothing is installed on a host, no live service is
touched, and no scenario here is deployment evidence.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from urllib import request as urlrequest
from urllib.error import HTTPError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import release_discovery as discovery
import release_identity as identity
import safe_update
import update_drain
import update_service
import update_staging
import update_transaction
from relay_helpers import PROBE_STUBS, TOKEN, free_port

ORIGIN = "https://github.com/Ami-Khokhar/agent-relay.git"
TAG = "0.5.0"
MARKER = "# release marker: the 0.5.0 tree differs from the installed one"
STAMP = "2026-11-11T00:00:00Z"
NOW = 1_770_000_000.0
TXN = "acceptance-txn"
SLEEPER = "import sys, time; time.sleep(float(sys.argv[-1])); print('drained')"
ECHO = "import sys; sys.stdout.write(sys.argv[1])"
# Sleeps briefly, then fails once with a durable mark, then succeeds: its
# retry runs while the update drain is held.
FLAKY = (
    "import os, sys, time, pathlib\n"
    "mark = pathlib.Path(os.environ['WF_MARK'])\n"
    "time.sleep(1.0)\n"
    "if mark.exists():\n"
    "    print('recovered')\n"
    "else:\n"
    "    mark.write_text('1')\n"
    "    sys.stderr.write('transient failure')\n"
    "    sys.exit(1)\n"
)


def _fixture_git(root, *args):
    """Fixture-housekeeping git with a private identity, never the updater's."""
    env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "f",
           "GIT_AUTHOR_EMAIL": "f@example.com", "GIT_COMMITTER_NAME": "f",
           "GIT_COMMITTER_EMAIL": "f@example.com", "HOME": os.environ.get("HOME", "")}
    result = subprocess.run(["/usr/bin/git", "-C", str(root), *args],
                            capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise AssertionError("fixture git %s failed: %s" % (args, result.stderr))
    return result.stdout


class _FakeTerminal:
    def __init__(self, answers, tty=True):
        self.answers, self.tty = list(answers), tty

    def is_tty(self):
        return self.tty

    def prompt(self, message):
        return self.answers.pop(0) if self.answers else ""

    def echo(self, message):
        pass


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _mode(path):
    return stat.S_IMODE(os.stat(str(path)).st_mode)


class _World:
    """One synthetic device: temporary HOME, state, install tree and release."""

    def __init__(self):
        self.base = Path(tempfile.mkdtemp(prefix="safe-update-acceptance-"))
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.state = self.base / "state"
        self.install = self._build_install()
        self.new_commit, self.manifest, self.manifest_bytes = self._build_release()
        self.old_commit = _fixture_git(self.install, "rev-parse", "HEAD").strip()
        self.fake_git = self._write_fake_git()
        self.github_calls = []

    def close(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def _build_install(self):
        """An ordinary, clean, user-owned checkout with the canonical remote."""
        install = self.base / "install"
        (install / "src").mkdir(parents=True)
        source_dir = Path(__file__).resolve().parent.parent / "src"
        for name in sorted(os.listdir(source_dir)):
            if name.endswith(".py"):
                shutil.copy2(source_dir / name, install / "src" / name)
        (install / "README.md").write_text("# agent-relay synthetic install\n")
        _fixture_git(install, "init", "-q", "-b", "main")
        _fixture_git(install, "add", "-A")
        _fixture_git(install, "commit", "-q", "-m", "installed revision")
        _fixture_git(install, "remote", "add", "origin", ORIGIN)
        return install.resolve()

    def _build_release(self):
        """The published 0.5.0 tree: one marked commit, tagged, on the origin."""
        bare = self.base / "origin.git"
        _fixture_git(self.base, "init", "-q", "--bare", str(bare))
        _fixture_git(self.install, "push", "-q", str(bare), "main:main")
        release_tree = self.base / "release"
        shutil.copytree(self.install, release_tree)
        server = release_tree / "src" / "server.py"
        server.write_text(server.read_text() + "\n" + MARKER + "\n")
        _fixture_git(release_tree, "add", "-A")
        _fixture_git(release_tree, "commit", "-q", "-m", "release %s" % TAG)
        commit = _fixture_git(release_tree, "rev-parse", "HEAD").strip()
        _fixture_git(release_tree, "tag", TAG)
        _fixture_git(release_tree, "push", "-q", str(bare), "HEAD:main")
        _fixture_git(release_tree, "push", "-q", str(bare), TAG)
        files = []
        for line in _fixture_git(release_tree, "ls-files", "-s").splitlines():
            mode, _blob, _stage, path = line.split(maxsplit=3)
            files.append({"path": path, "mode": mode,
                          "sha256": _digest(release_tree / path)})
        manifest = {"schema": identity.MANIFEST_SCHEMA, "repository": identity.REPOSITORY,
                    "version": TAG, "tag": TAG, "commit": commit,
                    "files": sorted(files, key=lambda entry: entry["path"])}
        raw = json.dumps(manifest, indent=1).encode("utf-8")
        return commit, manifest, raw

    def _write_fake_git(self):
        """A forwarding git: only the canonical clone URL is translated to the
        local fixture origin, so the updater's real git commands all run and
        the network is never contacted."""
        script = self.base / "fake-git"
        script.write_text(
            "#!/bin/bash\n"
            "for _a in \"$@\"; do echo \"arg $_a\" >> %s; done\n"
            "unset GIT_ALLOW_PROTOCOL\n"
            "args=(\"$@\")\n"
            "for ((i=0; i<${#args[@]}; i++)); do\n"
            "  [ \"${args[i]}\" = \"%s\" ] && args[i]=%s\n"
            "done\n"
            "exec /usr/bin/git -c protocol.file.allow=always \"${args[@]}\"\n"
            % (self.base / "git-boundary.log", ORIGIN, self.base / "origin.git"))
        script.chmod(0o755)
        return script

    def fetch_json(self, url):
        """The canonical GitHub HTTPS transport, answered in-process."""
        self.github_calls.append(url)
        if url == identity.RELEASE_BY_TAG_URL + TAG:
            release = {"id": 7, "tag_name": TAG, "draft": False, "prerelease": False,
                       "html_url": discovery.RELEASE_URL + TAG, "published_at": STAMP,
                       "target_commitish": "decoy-not-proof",
                       "assets": [{"id": 42, "name": identity.MANIFEST_NAME,
                                   "updated_at": STAMP}]}
            return json.dumps(release).encode("utf-8")
        if url == identity.TAG_REF_URL + TAG:
            return json.dumps({"object": {"sha": self.new_commit,
                                          "type": "commit"}}).encode("utf-8")
        if url == identity.ASSET_URL + "42":
            return self.manifest_bytes
        raise identity.UpdateError(
            "no published stable release matches this tag; check the tag name")

    def resolved_identity(self):
        return identity.resolve_release(TAG, fetch_json=self.fetch_json)[0]

    @contextlib.contextmanager
    def boundaries(self, terminal=None, now=None):
        """Wire the synthetic boundaries around one real CLI invocation."""
        patches = [
            patch.object(safe_update, "_state_dir", lambda: str(self.state)),
            patch.object(identity, "installation_path", lambda: str(self.install)),
            patch.object(safe_update, "_fetch", self.fetch_json),
            patch.dict(os.environ, {"HOME": str(self.home),
                                    "AGENT_RELAY_GIT": str(self.fake_git),
                                    "AGENT_RELAY_TOKEN": TOKEN}),
        ]
        if terminal is not None:
            patches.append(patch.object(identity, "_StdTerminal", lambda: terminal))
        if now is not None:
            patches.append(patch.object(safe_update.time, "time", lambda: now))
        for item in patches:
            item.start()
        try:
            yield
        finally:
            for item in reversed(patches):
                item.stop()

    def cli(self, *argv, terminal=None, now=None):
        """One real public CLI invocation; returns ``main()``'s exit code."""
        with self.boundaries(terminal=terminal, now=now):
            return safe_update.main(list(argv))

    def cli_text(self, *argv, **kwargs):
        """A CLI invocation with its stderr captured for refusal assertions."""
        err = StringIO()
        with contextlib.redirect_stderr(err):
            code = self.cli(*argv, **kwargs)
        return code, err.getvalue()

    def approve(self, now=None):
        terminal = _FakeTerminal([TAG, self.new_commit, "approve"])
        return self.cli("approve", TAG, terminal=terminal, now=now)

    def activate(self, transaction_id, restart, base_url):
        """The real activate command with the faked service manager injected."""
        with self.boundaries():
            return safe_update.cmd_activate(transaction_id, str(self.install),
                                            base_url=base_url, restart=restart)

    def recover(self, transaction_id, restart, base_url):
        with self.boundaries():
            return safe_update.cmd_recover(transaction_id, base_url=base_url,
                                           restart=restart)

    def head(self):
        return _fixture_git(self.install, "rev-parse", "HEAD").strip()

    def full_update(self, transaction_id, base_url):
        """Approve, stage and prepare-activation through the real CLI."""
        for name, code in (
                ("approve", self.approve()),
                ("stage", self.cli("stage", TAG, "--install-dir", str(self.install))),
                ("prepare-activation", self.cli(
                    "prepare-activation", TAG, "--install-dir", str(self.install),
                    "--base-url", base_url, "--transaction-id", transaction_id))):
            if code != 0:
                raise AssertionError("the %s step failed with %d" % (name, code))

    def unit_path(self):
        return self.home / ".config" / "systemd" / "user" / "agent-relay.service"


class _RelayBoundary:
    """The real relay HTTP server, treated as the per-user service it is.

    ``src/server.py`` runs for real from the install tree on a fixed loopback
    port. ``restart`` is the faked service-manager boundary: it stops the
    running relay process and spawns a fresh one from the same install tree on
    the same port with the same registry, token and state — what the per-user
    manager verbs would produce, without touching any host service.
    """

    def __init__(self, world):
        self.world = world
        self.dir = Path(tempfile.mkdtemp(prefix="acceptance-relay-"))
        self.agents_path = self.dir / "agents.json"
        registry = {"agents": [
            {"id": "slow", "command": sys.executable, "args": ["-c", SLEEPER]},
            {"id": "echo", "command": sys.executable, "args": ["-c", ECHO]},
            {"id": "flaky", "command": sys.executable, "args": ["-c", FLAKY],
             "env": {"WF_MARK": str(self.dir / "flaky-mark")}},
        ]}
        self.agents_path.write_text(json.dumps(registry))
        self.probes = {}
        for name, body in PROBE_STUBS.items():
            path = self.dir / name
            path.write_text(body)
            os.chmod(str(path), 0o755)
            self.probes["AGENT_RELAY_" + name.upper()] = str(path)
        self.port = free_port()
        self.base = "http://127.0.0.1:%d" % self.port
        self.script = str(world.install / "src" / "server.py")
        self.proc = None
        self.restarts = []
        self.write_unit()

    def write_unit(self):
        """The supported systemd --user definition, naming this install only."""
        path = self.world.unit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        environment = [
            "A2A_RELAY_PORT=%d" % self.port,
            "A2A_AGENTS_FILE=%s" % self.agents_path,
            "AGENT_RELAY_TOKEN=%s" % TOKEN,
            "AGENT_RELAY_STATE_DIR=%s" % self.world.state,
            "AGENT_RELAY_PRESSURE_GUARD=0",
            "AGENT_RELAY_SYSCTL=%s" % self.probes["AGENT_RELAY_SYSCTL"],
            "AGENT_RELAY_PMSET=%s" % self.probes["AGENT_RELAY_PMSET"],
        ]
        path.write_text("[Unit]\nDescription=agent-relay\n\n[Service]\n"
                        "ExecStart=%s %s\nWorkingDirectory=%s\n%s\n" % (
                            sys.executable, self.script, self.world.install,
                            "\n".join("Environment=%s" % item for item in environment)))
        os.chmod(str(path), 0o600)

    def _env(self):
        env = {**os.environ, "A2A_RELAY_PORT": str(self.port),
               "A2A_AGENTS_FILE": str(self.agents_path), "AGENT_RELAY_TOKEN": TOKEN,
               "AGENT_RELAY_STATE_DIR": str(self.world.state),
               "AGENT_RELAY_PRESSURE_GUARD": "0", **self.probes}
        for name in ("AGENT_RELAY_TEMP_READER", "AGENT_RELAY_TEMP_WARN_C",
                     "AGENT_RELAY_GIT"):
            env.pop(name, None)
        return env

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, self.script], cwd=str(self.world.install),
            env=self._env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.wait_ready()

    def wait_ready(self):
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.proc.poll() is not None:
                _, stderr = self.proc.communicate()
                raise RuntimeError("relay exited %d: %s"
                                   % (self.proc.returncode,
                                      stderr.decode("utf-8", "replace")))
            try:
                with urlrequest.urlopen(self.base + "/healthz", timeout=0.5) as answer:
                    if answer.status == 200:
                        return
            except Exception:  # noqa: BLE001 - retry until the listener is up
                time.sleep(0.03)
        raise RuntimeError("relay did not start")

    def restart(self, service):
        """The faked service-manager boundary, shaped like restart_service."""
        if not isinstance(service, dict) or service.get("kind") not in ("launchd", "systemd"):
            raise update_service.ServiceError(
                "restart needs a receipt from inspect_service")
        self.restarts.append({"kind": service["kind"], "path": service["path"]})
        self.stop_proc()
        self.start()
        return {"kind": service["kind"], "restartedAt": time.time()}

    def refusing_restart(self, service):
        """A service manager that refuses, without restarting anything."""
        self.restarts.append({"kind": service["kind"], "refused": True})
        raise update_service.ServiceError("the systemd --user manager refused the restart")

    def stop_proc(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()
        self.proc = None

    def close(self):
        self.stop_proc()
        shutil.rmtree(self.dir, ignore_errors=True)

    def request(self, method, path, body=None, token=TOKEN):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"content-type": "application/json"} if data else {}
        if token:
            headers["authorization"] = "Bearer " + token
        request = urlrequest.Request(self.base + path, data=data, method=method,
                                     headers=headers)
        try:
            with urlrequest.urlopen(request, timeout=10) as answer:
                return answer.status, json.loads(answer.read().decode("utf-8") or "null")
        except HTTPError as exc:
            payload = exc.read().decode("utf-8", "replace")
            return exc.code, json.loads(payload or "null")

    def submit(self, **body):
        return self.request("POST", "/v1/tasks", body)

    def wait_task(self, task_id, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _status, task = self.request("GET", "/v1/tasks/%s" % task_id)
            if task.get("status") not in ("queued", "running"):
                return task
            time.sleep(0.03)
        raise RuntimeError("task did not finish")

    def revision(self):
        """The relay's own authenticated answer: the exact full commit."""
        _status, body = self.request("GET", "/v1/agents")
        return body["revision"]["commit"]

    def wait_workflow(self, workflow_id, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _status, view = self.request("GET", "/v1/workflows/%s" % workflow_id)
            if view.get("status") in ("completed", "failed", "cancelled"):
                return view
            time.sleep(0.05)
        raise RuntimeError("workflow did not finish")


class _Acceptance(unittest.TestCase):
    def setUp(self):
        self.world = _World()
        self.relay = _RelayBoundary(self.world)
        # The device exists before the relay boots: a relay whose private
        # update state cannot yet be read would refuse admission at startup.
        identity.private_state_dir(Path(self.world.state), "acceptance state")
        identity.ensure_device_id(Path(self.world.state))
        self.relay.start()
        self.addCleanup(self.relay.close)
        self.addCleanup(self.world.close)

    def snapshot(self, path):
        path = Path(path)
        return {"digest": _digest(path), "mode": _mode(path)}

    def external_state_paths(self):
        """The external state an activation must preserve byte for byte."""
        return {
            "consent": Path(self.world.state) / "consent.json",
            "stage receipt": Path(self.journal()["stage"]["path"])
            / update_staging.STAGE_RECEIPT_NAME,
            "service unit": self.world.unit_path(),
            "registry": self.relay.agents_path,
        }

    def journal(self):
        return update_transaction.read_journal(state_dir=self.world.state)

    def barrier(self):
        return update_transaction.read_barrier(state_dir=self.world.state)

    def wait_quiesced(self, transaction_id=TXN, timeout=20):
        record = self.journal()
        drain = record["drain"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = update_drain.drain_status(
                self.relay.base, transaction_id, token=TOKEN,
                revision=drain["revision"], capability=drain["capability"])
            if status.get("quiesced"):
                return
            time.sleep(0.05)
        self.fail("the held drain never reported quiescence")


class ApprovedUpdateTest(_Acceptance):
    def test_an_approved_release_drains_and_activates_end_to_end(self):
        """Approve → stage → held drain → exact activation on the real relay.

        The full public flow runs against the real HTTP relay and workflow
        engine: the held drain refuses new external admissions while an
        accepted workflow keeps going through its retry gap and children, and
        the activation swaps the source, restarts the (faked) service once and
        verifies the exact new full commit through the authenticated boundary.
        """
        world, relay = self.world, self.relay
        self.assertEqual(world.cli("status"), 0)
        self.assertEqual(world.cli("inspect", TAG), 0)
        self.assertEqual(relay.revision(), world.old_commit,
                         "the relay runs the exact installed full commit")

        self.assertEqual(world.approve(), 0)
        self.assertEqual(world.cli("stage", TAG, "--install-dir", str(world.install)), 0)
        self.assertEqual(world.head(), world.old_commit,
                         "staging never wrote to the active checkout")

        # The rehearsal verifies the stage and consent and releases again.
        self.assertEqual(world.cli("drain", TAG, "--install-dir", str(world.install),
                                   "--base-url", relay.base), 0)
        self.assertEqual(relay.submit(agentId="echo", input="0.05")[0], 202,
                         "the rehearsal released its hold")

        # Accepted work: a flaky workflow whose retry and child run later, one
        # slow task the drain must wait for, and one run kept for a resume.
        _status, submitted = relay.request("POST", "/v1/workflows", {"steps": [
            {"id": "first", "agentId": "flaky", "input": "attempt", "retries": 3},
            {"id": "second", "agentId": "echo", "input": "hello", "needs": ["first"]}]})
        self.assertEqual(_status, 202)
        _status, slow = relay.submit(agentId="slow", input="1.0")
        self.assertEqual(_status, 202)
        _status, kept = relay.request("POST", "/v1/workflows", {"steps": [
            {"id": "work", "agentId": "slow", "input": "5"}]})
        self.assertEqual(_status, 202)

        # Prepare: the exact-revision drain is acquired and held across return.
        self.assertEqual(world.cli("prepare-activation", TAG, "--install-dir",
                                   str(world.install), "--base-url", relay.base,
                                   "--transaction-id", TXN), 0)
        record = self.journal()
        self.assertEqual(record["phase"], "prepared")
        self.assertEqual(record["revision"], world.old_commit)
        self.assertEqual(self.barrier()["transactionId"], TXN)
        self.assertEqual(self.barrier()["revision"], world.old_commit)
        self.assertEqual(update_drain._server_revision(relay.base), world.old_commit)

        # While the drain is held: every new external admission refuses.
        self.assertEqual(relay.submit(agentId="echo", input="new")[0], 503)
        self.assertEqual(relay.request("POST", "/v1/workflows", {"steps": [
            {"id": "x", "agentId": "echo", "input": "bang"}]})[0], 503)

        # The accepted work is not interrupted: the retry gap and the child
        # step run through the engine's own path, and the slow task finishes.
        view = relay.wait_workflow(submitted["id"])
        self.assertEqual(view["status"], "completed")
        steps = {step["id"]: step for step in view["steps"]}
        self.assertEqual(steps["first"]["attempts"], 2,
                         "the retry ran while the drain was held")
        self.assertEqual(steps["second"]["output"], "hello")

        # The kill button keeps the journal of the still-held run for a resume.
        _status, stopped = relay.request("POST", "/v1/pressure/kill")
        self.assertIn(kept["id"], stopped["killed"])
        self.assertEqual(relay.request(
            "POST", "/v1/workflows/%s/resume" % kept["id"])[0], 503,
            "resume refuses while the update drain is held")
        relay.wait_task(slow["id"])
        self.wait_quiesced()
        self.assertEqual(relay.submit(agentId="echo", input="still")[0], 503,
                         "the hold persists until the activation releases it")

        paths = self.external_state_paths()
        before = {name: self.snapshot(path) for name, path in paths.items()}
        self.assertEqual(world.activate(TXN, relay.restart, relay.base), 0)

        # The exact new revision, verified at the authenticated boundary.
        self.assertEqual(world.head(), world.new_commit)
        self.assertEqual(self.relay.revision(), world.new_commit)
        self.assertEqual(update_drain._server_revision(relay.base), world.new_commit)
        self.assertEqual(len(relay.restarts), 1, "exactly one service restart")
        self.assertEqual(relay.restarts[0]["kind"], "systemd")
        self.assertIsNone(self.journal(), "the finished record is expunged")
        self.assertIsNone(self.barrier(), "the admission barrier is removed")
        self.assertEqual(relay.submit(agentId="echo", input="open")[0], 202,
                         "admission reopens after the verified activation")
        self.assertEqual(world.github_calls[-3:], [
            identity.RELEASE_BY_TAG_URL + TAG, identity.TAG_REF_URL + TAG,
            identity.ASSET_URL + "42"],
            "prepare re-resolved exactly the canonical release identity")
        for name, path in paths.items():
            self.assertEqual(self.snapshot(path), before[name],
                             "%s is byte- and permission-preserved" % name)

    def test_a_failed_activation_rolls_back_to_the_exact_prior_revision(self):
        """A service manager that refuses after the swap is rolled back whole."""
        world, relay = self.world, self.relay
        world.full_update(TXN, relay.base)
        self.assertEqual(world.activate(TXN, relay.refusing_restart, relay.base), 1)
        self.assertEqual(world.head(), world.old_commit,
                         "the checkout was restored to the journaled prior revision")
        self.assertEqual(self.relay.revision(), world.old_commit,
                         "the relay still serves the exact prior revision")
        self.assertEqual(relay.submit(agentId="echo", input="open")[0], 202,
                         "admission reopened and the drain was released")
        self.assertIsNone(self.journal())
        self.assertIsNone(self.barrier())
        self.assertEqual(relay.restarts, [{"kind": "systemd", "refused": True}],
                         "the relay was never restarted")

    def test_an_interrupted_activation_is_recovered_and_the_barrier_fails_closed(self):
        """A crash mid-activation leaves a closed barrier; recover finishes it.

        The interruption is the durable state a real crash leaves: the swap
        done, the journal walked to its ``recovery_required`` end exactly as
        the activation records one. A restarted relay boots inside the barrier
        and refuses admission while naming the transaction; ``recover`` then
        restores the exact prior revision, restarts the (faked) service once,
        and reopens admission.
        """
        world, relay = self.world, self.relay
        world.full_update(TXN, relay.base)
        record = update_transaction.recover_transaction(state_dir=world.state)
        update_transaction.transition(TXN, "prepared", "draining",
                                      state_dir=world.state, quiesced=True)
        update_transaction.transition(TXN, "draining", "quiescent",
                                      state_dir=world.state,
                                      service={"kind": "systemd"})
        update_transaction.transition(TXN, "quiescent", "activating",
                                      state_dir=world.state,
                                      oldRevision=world.old_commit,
                                      targetCommit=world.new_commit,
                                      servicePath=str(world.unit_path()))
        with world.boundaries():
            update_staging.activate_transaction_source(
                record, git=update_staging.Git(update_staging.default_git()))
        update_transaction.transition(TXN, "activating", "restarted",
                                      state_dir=world.state,
                                      commit=world.new_commit)
        update_transaction.transition(TXN, "restarted", "recovery_required",
                                      state_dir=world.state,
                                      reason="simulated interruption")
        self.assertEqual(world.head(), world.new_commit)

        # The crashed relay "came back up": it boots inside the barrier.
        relay.restart({"kind": "systemd", "path": str(world.unit_path())})
        self.assertEqual(self.relay.revision(), world.new_commit)
        self.assertEqual(relay.submit(agentId="echo", input="x")[0], 503,
                         "the restarted relay refuses admission inside the barrier")
        _status, state = relay.request("GET", "/v1/admin/update/state")
        self.assertTrue(state["admissionClosed"])
        self.assertEqual(state["barrier"]["transactionId"], TXN,
                         "the barrier names the owning transaction")
        self.assertIn(str(world.old_commit), state["barrier"].get("reason", ""),
                      "the refusal names the revision the barrier binds")

        self.assertEqual(world.recover(TXN, relay.restart, relay.base), 0)
        self.assertEqual(world.head(), world.old_commit,
                         "the journal's exact prior revision was restored")
        self.assertEqual(self.relay.revision(), world.old_commit,
                         "the restarted relay serves the restored revision")
        self.assertEqual(len(relay.restarts), 2,
                         "one crash restart plus exactly one recovery restart")
        self.assertIsNone(self.journal())
        self.assertIsNone(self.barrier())
        self.assertEqual(relay.submit(agentId="echo", input="open")[0], 202,
                         "admission reopened after the verified recovery")


class RefusalTest(_Acceptance):
    def test_expired_wrong_device_and_replayed_consent_fail_closed(self):
        """Guard: the 15-minute, single-use, device-local consent holds at the
        effect boundary; every bad consumption leaves the record unusable and
        stages nothing."""
        world = self.world
        self.assertEqual(world.approve(), 0)
        consent_path = Path(world.state) / "consent.json"
        self.assertEqual(_mode(consent_path), 0o600)
        original = consent_path.read_bytes()
        issued = json.loads(original)["issuedAt"]
        # Expired: a consent older than its 15 minutes is refused, not consumed.
        code, err = world.cli_text("stage", TAG, "--install-dir", str(world.install),
                                   now=issued + identity.CONSENT_MAX_AGE + 1)
        self.assertEqual(code, 1)
        self.assertIn("consent has expired", err)
        self.assertEqual(consent_path.read_bytes(), original)
        # Wrong device: a record from another device never stages here.
        consent = json.loads(original)
        consent["device"] = "f" * 32
        consent_path.write_text(json.dumps(consent, indent=1))
        os.chmod(str(consent_path), 0o600)
        code, err = world.cli_text("stage", TAG, "--install-dir", str(world.install))
        self.assertEqual(code, 1)
        self.assertIn("different device", err)
        consent_path.write_bytes(original)
        os.chmod(str(consent_path), 0o600)
        # A valid stage consumes the consent exactly once; the replay refuses.
        self.assertEqual(world.cli("stage", TAG, "--install-dir", str(world.install)), 0)
        self.assertTrue(json.loads(consent_path.read_bytes())["consumed"])
        code, err = world.cli_text("stage", TAG, "--install-dir", str(world.install))
        self.assertEqual(code, 1)
        self.assertIn("already consumed", err)
        self.assertEqual(world.head(), world.old_commit,
                         "nothing moved: only a stage was built and never activated")

    def test_a_missing_release_and_a_noninteractive_terminal_fail_closed(self):
        """Guard: no release is a clear refusal, approval needs a real TTY, and
        an activation without a prepared transaction refuses before any effect."""
        world = self.world
        code, err = world.cli_text("inspect", "9.9.9")
        self.assertEqual(code, 1)
        self.assertIn("no published stable release matches this tag", err)
        self.assertEqual(world.github_calls, [identity.RELEASE_BY_TAG_URL + "9.9.9"],
                         "the wrong tag asked GitHub once and stopped")
        code, err = world.cli_text("approve", TAG, terminal=_FakeTerminal([], tty=False))
        self.assertEqual(code, 1)
        self.assertIn("interactive terminal", err)
        self.assertFalse((Path(world.state) / "consent.json").exists(),
                         "a refused approval records nothing")
        code, err = world.cli_text("activate", TXN, "--install-dir", str(world.install))
        self.assertEqual(code, 1)
        self.assertIn("no update transaction is recorded", err)
        self.assertEqual(world.head(), world.old_commit)

    def test_a_broken_stage_refuses_preparation_and_never_holds_admission(self):
        """Guard: a stage that no longer verifies refuses preparation before
        any drain is acquired, so admission never closes on a refused flow."""
        world, relay = self.world, self.relay
        self.assertEqual(world.approve(), 0)
        self.assertEqual(world.cli("stage", TAG, "--install-dir", str(world.install)), 0)
        staging = Path(world.state) / "staging"
        victim = next(child for child in sorted(staging.iterdir())
                      if (child / update_staging.STAGE_RECEIPT_NAME).exists())
        tracked = victim / "src" / "server.py"
        original = tracked.read_bytes()
        tracked.write_bytes(original + b"\n# mutated after staging\n")
        code, err = world.cli_text("prepare-activation", TAG, "--install-dir",
                                   str(world.install), "--base-url", relay.base,
                                   "--transaction-id", TXN)
        self.assertEqual(code, 1)
        self.assertIn("no longer verifies", err)
        self.assertIsNone(self.journal())
        self.assertEqual(relay.submit(agentId="echo", input="x")[0], 202,
                         "no hold was acquired, so admission stayed open")
        tracked.write_bytes(original)
        self.assertEqual(world.cli("prepare-activation", TAG, "--install-dir",
                                   str(world.install), "--base-url", relay.base,
                                   "--transaction-id", TXN), 0)
        self.assertEqual(self.journal()["phase"], "prepared")


class AbortAndRejectionTest(_Acceptance):
    def test_simultaneous_submissions_refuse_and_recover_abort_reopens(self):
        """Guard: while the drain is held, racing submissions refuse as one
        drain refusal, resumes refuse, and recover-abort reopens admission
        only after the full proof."""
        world, relay = self.world, self.relay
        _status, running = relay.request("POST", "/v1/workflows", {"steps": [
            {"id": "work", "agentId": "slow", "input": "5"}]})
        self.assertEqual(_status, 202)
        world.full_update(TXN, relay.base)
        _status, stopped = relay.request("POST", "/v1/pressure/kill")
        self.assertIn(running["id"], stopped["killed"])
        self.wait_quiesced()
        results = []
        lock = threading.Lock()

        def race():
            for _ in range(4):
                result = relay.submit(agentId="echo", input="0.05")
                with lock:
                    results.append(result)
                time.sleep(0.01)

        threads = [threading.Thread(target=race) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual({status for status, _body in results}, {503},
                         "every racing submission refuses while the drain is held")
        self.assertEqual(relay.request(
            "POST", "/v1/workflows/%s/resume" % running["id"])[0], 503)
        self.assertEqual(relay.request("POST", "/v1/workflows", {"steps": [
            {"id": "x", "agentId": "echo", "input": "bang"}]})[0], 503)
        self.assertEqual(self.journal()["phase"], "prepared")
        self.assertEqual(world.cli("recover-abort", TXN, "--base-url", relay.base), 0)
        self.assertIsNone(self.journal())
        self.assertIsNone(self.barrier())
        self.assertEqual(world.head(), world.old_commit,
                         "nothing was ever activated")
        self.assertEqual(self.relay.revision(), world.old_commit)
        self.assertEqual(relay.submit(agentId="echo", input="0.05")[0], 202,
                         "admission reopens after the proven abort")
        self.assertEqual(relay.request(
            "POST", "/v1/workflows/%s/resume" % running["id"])[0], 202,
            "resume works again after the release")


if __name__ == "__main__":
    unittest.main()
