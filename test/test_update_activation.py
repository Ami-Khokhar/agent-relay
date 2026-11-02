"""Exact-revision activation through the restricted per-user service boundary (#158).

The activation consumer walks a prepared transaction through its journaled
phases: it proves the drain, the stage, the checkout and the per-user service
definition before any effect; swaps the source with the journaled exact
git activation; restarts the service; and verifies the restarted relay only
through an authenticated endpoint carrying the exact full commit plus the
transaction's own barrier ownership. Test boundaries are faked at the service
manager, the HTTP endpoints and the participating git objects; the tests
never touch a user's own services, and the command spies show no installer,
no main pull, no root control and no force-kill.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import release_identity as identity
import safe_update
import update_drain
import update_service
import update_staging
import update_transaction

TAG = "0.5.0"
NOW = 1_770_000_000.0
STAMP = "2026-10-01T00:00:00Z"
ORIGIN = "https://github.com/Ami-Khokhar/agent-relay.git"
CAPABILITY = "c" * 64
TXN = "txn-activate"


def _run_git(*args, cwd=None):
    """Run /usr/bin/git with the fixture's args; the fixture repo is named
    absolutely when a -C context is needed, otherwise plain git."""
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "f", "GIT_AUTHOR_EMAIL": "f@example.com",
           "GIT_COMMITTER_NAME": "f", "GIT_COMMITTER_EMAIL": "f@example.com"}
    result = subprocess.run(["/usr/bin/git", *args],
                            capture_output=True, text=True, env=env, cwd=cwd)
    if result.returncode != 0:
        raise AssertionError("fixture git %s failed: %s" % (args, result.stderr))
    return result.stdout.strip()


class _Fixture:
    """One clean canonical checkout at the old commit, one verified stage at the
    target commit, untouched external state and one prepared transaction."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.state = base / "state"
        self.repo = base / "relay-checkout"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "README.md").write_text("# agent-relay old\n")
        (self.repo / "src" / "server.py").write_text("# relay server old\n")
        (self.repo / "src" / "safe_update.py").write_text("# updater\n")
        _run_git("-C", str(self.repo), "init", "-q", "-b", "main")
        _run_git("-C", str(self.repo), "add", "-A")
        _run_git("-C", str(self.repo), "commit", "-q", "-m", "old release")
        self.old = _run_git("-C", str(self.repo), "rev-parse", "HEAD")
        _run_git("-C", str(self.repo), "remote", "add", "origin", ORIGIN)
        # the stage: a real private repository detached at the target commit
        self.stage = base / "stage"
        _run_git("clone", "-q", "--no-local", str(self.repo), str(self.stage))
        (self.stage / "README.md").write_text("# agent-relay new\n")
        (self.stage / "src" / "server.py").write_text("# relay server new\n")
        _run_git("-C", str(self.stage), "add", "-A")
        _run_git("-C", str(self.stage), "commit", "-q", "-m", "target release")
        _run_git("-C", str(self.stage), "checkout", "-q", "--detach")
        self.target = _run_git("-C", str(self.stage), "rev-parse", "HEAD")
        files = []
        for rel in ("README.md", "src/server.py", "src/safe_update.py"):
            data = (self.stage / rel).read_bytes()
            files.append({"path": rel, "mode": "100644",
                          "sha256": hashlib.sha256(data).hexdigest()})
        manifest = {"schema": identity.MANIFEST_SCHEMA, "repository": identity.REPOSITORY,
                    "version": TAG, "tag": TAG, "commit": self.target, "files": files}
        manifest_bytes = json.dumps(manifest).encode("utf-8")
        self.identity = {"repository": identity.REPOSITORY, "releaseId": 9, "tag": TAG,
                         "commit": self.target, "assetId": 44,
                         "manifestDigest": hashlib.sha256(manifest_bytes).hexdigest(),
                         "publishedAt": STAMP, "assetUpdatedAt": STAMP}
        self.state.mkdir(mode=0o700)
        identity.ensure_device_id(self.state)
        self.install_patch = patch.object(identity, "installation_path",
                                          lambda: self.repo.resolve())
        self.install_patch.start()
        consent = {
            "schema": identity.CONSENT_SCHEMA, "repository": identity.REPOSITORY,
            "tag": TAG, "commit": self.target,
            "identityDigest": identity.identity_digest(self.identity),
            "identity": self.identity,
            "installation": str(self.repo.resolve()),
            "device": (self.state / "device-id").read_text(), "nonce": "a" * 32,
            "issuedAt": NOW,
            "expiresAt": NOW + 900, "consumed": True, "consumedAt": NOW + 60,
        }
        path = self.state / "consent.json"
        path.write_text(json.dumps(consent, indent=1))
        os.chmod(str(path), 0o600)
        receipt = {"schema": update_staging.STAGE_SCHEMA,
                   "identity": self.identity,
                   "identityDigest": identity.identity_digest(self.identity),
                   "manifestB64": base64.b64encode(manifest_bytes).decode("ascii"),
                   "sourceRoot": str(self.repo.resolve()),
                   "sourceCommit": self.old,
                   "stage": str(self.stage),
                   "git": "/usr/bin/git",
                   "createdAt": NOW + 120}
        receipt_path = self.stage / "stage-receipt.json"
        receipt_path.write_text(json.dumps(receipt, indent=1))
        os.chmod(str(receipt_path), 0o600)
        begin = dict(receipt)
        begin["transactionId"] = TXN
        begin["drain"] = {"revision": self.old, "capability": CAPABILITY}
        update_transaction.begin_transaction(begin, state_dir=self.state)
        # a fake HOME for the service definition the tests inspect
        self.home = base / "home"
        (self.home / "Library" / "LaunchAgents").mkdir(parents=True)
        return self

    def __exit__(self, *args):
        self.install_patch.stop()
        self.tmp.cleanup()
        return False

    def write_plist(self, **overrides):
        definition = {"Label": "com.agent-relay",
                      "ProgramArguments": [sys.executable,
                                           str(self.repo / "src" / "server.py")],
                      "WorkingDirectory": str(self.repo.resolve()), "RunAtLoad": True,
                      "KeepAlive": True, "ProcessType": "Background",
                      "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}
        definition.update(overrides)
        path = self.home / "Library" / "LaunchAgents" / "com.agent-relay.plist"
        with open(str(path), "wb") as handle:
            plistlib.dump(definition, handle)
        return path, definition

    def restore_plist(self):
        self.write_plist()


class _Case(unittest.TestCase):
    quiesced = True

    def setUp(self):
        self.fixture = _Fixture()
        self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        self.state, self.repo = self.fixture.state, self.fixture.repo
        self.calls, self.restart_commands = [], []
        self._patch(update_drain, "drain_status", self._fake_drain_status)
        self._patch(update_drain, "_server_revision", lambda _base: self.fixture.old)
        self._patch(safe_update, "_refresh_barrier", lambda _b, _t: {"admissionClosed": False})
        self.home_patch = patch.dict(os.environ, {"HOME": str(self.fixture.home)})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self._patch(safe_update, "_state_dir", lambda: self.state)
        self.env_patch = patch.dict(os.environ, {
            "AGENT_RELAY_URL": "http://127.0.0.1-fake", "AGENT_RELAY_TOKEN": "t" * 32})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _patch(self, obj, name, replacement):
        patcher = patch.object(obj, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_drain_status(self, _base, transaction_id, *, token=None, revision=None,
                           capability=None, timeout=None):
        self.calls.append(("status", transaction_id))
        if transaction_id != TXN or revision != self.fixture.old or capability != CAPABILITY:
            raise update_drain.DrainError("revision_mismatch", "refused")
        return {"quiesced": self.quiesced, "active": 0, "queued": 0, "workflows": 0,
                "transactionId": transaction_id, "revision": revision}

    def fake_inspect(self, _root):
        self.calls.append(("inspect", _root))
        path, definition = self.fixture.write_plist()
        return {"kind": "launchd", "label": "com.agent-relay", "path": str(path),
                "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
                "programArguments": list(definition["ProgramArguments"]),
                "environment": {}}

    def fake_restart(self, service):
        self.calls.append(("restart", service["kind"], service["path"]))
        argv = ["/usr/bin/fake-manager", "--user", "restart", service["label"]]
        self.restart_commands.append(argv)
        return {"kind": service["kind"], "commands": [argv], "restartedAt": 1.0}

    def _fake_authenticated_get(self, _base, _token, path):
        self.calls.append(("get", path))
        if path == safe_update.BARRIER_PATH:
            journal = update_transaction.read_journal(state_dir=self.state)
            if journal is not None and journal["transactionId"] == TXN:
                return {"admissionClosed": True,
                        "barrier": {"transactionId": TXN, "reason": "activation live"}}
            return {"admissionClosed": False, "barrier": None}
        return {"agents": [],
                "revision": {"commit": self.fixture.target if self.restart_commands
                             else self.fixture.old}}


class ServiceInspectionTest(_Case):
    """inspect_service only accepts positively validated per-user definitions."""

    def test_supported_plist_is_accepted_with_preserved_arguments(self):
        self.fixture.write_plist()
        receipt = update_service.inspect_service(self.repo)
        self.assertEqual(receipt["kind"], "launchd")
        self.assertEqual(receipt["label"], "com.agent-relay")
        self.assertIn(str(self.repo.resolve() / "src" / "server.py"),
                      receipt["programArguments"][1])
        self.assertTrue(receipt["digest"])

    def test_missing_definition_is_refused(self):
        with self.assertRaises(identity.UpdateError) as caught:
            update_service.inspect_service(self.repo)
        self.assertIn("no per-user", str(caught.exception))

    def test_symlinked_definition_is_refused(self):
        real, _ = self.fixture.write_plist()
        moved = real.with_name("original.agent-relay.plist")
        real.rename(moved)
        symlink = self.fixture.home / "Library" / "LaunchAgents" / "com.agent-relay.plist"
        symlink.symlink_to(moved)
        with self.assertRaises(identity.UpdateError) as caught:
            update_service.inspect_service(self.repo)
        self.assertIn("symlink", str(caught.exception))

    def test_group_writable_definition_is_refused(self):
        path, _ = self.fixture.write_plist()
        os.chmod(str(path), 0o664)
        with self.assertRaises(identity.UpdateError):
            update_service.inspect_service(self.repo)

    def test_ambiguous_definitions_are_refused(self):
        self.fixture.write_plist()
        unit = self.fixture.home / ".config" / "systemd" / "user" / "agent-relay.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Service]\n")
        with self.assertRaises(identity.UpdateError) as caught:
            update_service.inspect_service(self.repo)
        self.assertIn("ambiguous", str(caught.exception))

    def test_foreign_and_shell_and_sudo_definitions_are_refused(self):
        self.fixture.write_plist(ProgramArguments=[sys.executable,
                                                   "/opt/other/src/server.py"])
        with self.assertRaises(identity.UpdateError) as caught:
            update_service.inspect_service(self.repo)
        self.assertIn("foreign or unsupported", str(caught.exception))
        self.fixture.write_plist(ProgramArguments=["/bin/sh", "-c",
                                                   str(self.repo / "src" / "server.py")])
        with self.assertRaises(identity.UpdateError):
            update_service.inspect_service(self.repo)
        self.fixture.write_plist(ProgramArguments=["/usr/bin/sudo", sys.executable,
                                                   str(self.repo / "src" / "server.py")])
        with self.assertRaises(identity.UpdateError):
            update_service.inspect_service(self.repo)

    def test_non_python_interpreters_are_refused(self):
        self.fixture.write_plist(ProgramArguments=["/bin/date",
                                                   str(self.repo / "src" / "server.py")])
        with self.assertRaises(identity.UpdateError) as caught:
            update_service.inspect_service(self.repo)
        self.assertIn("Python interpreter", str(caught.exception))

    def test_config_inside_the_checkout_is_refused(self):
        self.fixture.write_plist(ProgramArguments=[sys.executable,
                                                   str(self.repo / "src" / "server.py"),
                                                   "--config", str(self.repo / "relay.ini")])
        with self.assertRaises(identity.UpdateError) as caught:
            update_service.inspect_service(self.repo)
        self.assertIn("external configuration", str(caught.exception))

    def test_a_working_directory_away_from_the_install_is_refused(self):
        self.fixture.write_plist(WorkingDirectory=str(self.fixture.home))
        with self.assertRaises(identity.UpdateError):
            update_service.inspect_service(self.repo)


class ServiceRestartTest(_Case):
    """The restart boundary only runs the plain per-user manager verbs."""

    def _patched_restart(self):
        commands = []

        def run(argv, **_kwargs):
            commands.append(list(argv))

            class _Result:
                returncode, stdout, stderr = 0, "", ""
            return _Result()
        return commands, run

    def test_launchd_restart_runs_bootout_and_bootstrap_without_any_force_or_root(self):
        commands, run = self._patched_restart()
        self.fixture.write_plist()
        service = update_service.inspect_service(self.repo)
        with patch.object(update_service, "_manager_executable",
                          lambda names, label: names[0]), \
                patch.object(update_service.subprocess, "run", run):
            receipt = update_service.restart_service(service)
        self.assertEqual(receipt["kind"], "launchd")
        self.assertEqual([argv[0] for argv in commands], ["/bin/launchctl", "/bin/launchctl"])
        verbs = [argv[1] for argv in commands]
        self.assertIn("bootout", verbs)
        self.assertIn("bootstrap", verbs)
        for argv in commands:
            joined = " ".join(argv)
            self.assertNotIn("sudo", joined)
            self.assertNotIn("-k", " ".join(argv[1:]),
                             "the restart never force-kills under a held quiescence")
            self.assertNotIn("kill", joined)
            self.assertNotIn("bootstrapper-installer", joined)
            self.assertNotIn("pip", joined), self.assertNotIn("git pull", joined)

    def test_systemd_restart_uses_the_user_scope(self):
        commands, run = self._patched_restart()
        (self.fixture.home / "Library" / "LaunchAgents" / "com.agent-relay.plist").unlink(missing_ok=True)
        unit = self.fixture.home / ".config" / "systemd" / "user" / "agent-relay.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text("[Service]\nExecStart=%s %s\nWorkingDirectory=%s\n"
                        % (sys.executable, str(self.repo / "src" / "server.py"),
                           str(self.repo.resolve())))
        service = update_service.inspect_service(self.repo)
        with patch.object(update_service, "_manager_executable",
                          lambda names, label: names[0]), \
                patch.object(update_service.subprocess, "run", run):
            update_service.restart_service(service)
        self.assertEqual(commands, [["/usr/bin/systemctl", "--user", "restart",
                                     "agent-relay.service"]])

    def test_root_is_refused_and_runs_nothing(self):
        commands, run = self._patched_restart()
        self.fixture.write_plist()
        service = update_service.inspect_service(self.repo)
        with patch.object(os, "geteuid", lambda: 0):
            with self.assertRaises(identity.UpdateError) as caught:
                update_service.restart_service(service)
        self.assertIn("root", str(caught.exception))
        self.assertEqual(commands, [])


class AwaitRevisionTest(_Case):
    """await_revision reads only an exact full commit through the caller's probe."""

    def test_polls_until_the_exact_commit_arrives(self):
        answers = iter([{"revision": {"commit": self.fixture.old}},
                        {"revision": {"commit": self.fixture.target}}])
        body = update_service.await_revision(self.fixture.target,
                                             authenticated_request=lambda: next(answers),
                                             deadline_s=1.0)
        self.assertEqual(body["revision"]["commit"], self.fixture.target)

    def test_unknown_health_is_refused_immediately(self):
        with self.assertRaises(identity.UpdateError) as caught:
            update_service.await_revision(self.fixture.target,
                                          authenticated_request=lambda: {"ok": True},
                                          deadline_s=1.0)
        self.assertIn("unknown health", str(caught.exception))

    def test_a_wrong_commit_never_passes_and_the_deadline_is_hard(self):
        with self.assertRaises(identity.UpdateError) as caught:
            update_service.await_revision(
                self.fixture.target,
                authenticated_request=lambda: {"revision": {"commit": self.fixture.old}},
                deadline_s=0.4)
        self.assertIn("never reported the exact commit", str(caught.exception))

    def test_transport_failures_are_waited_out(self):
        calls = []

        def request():
            calls.append(1)
            if len(calls) < 3:
                raise update_drain.DrainError("drain_unreachable", "not yet")
            return {"revision": {"commit": self.fixture.target}}
        body = update_service.await_revision(self.fixture.target,
                                             authenticated_request=request,
                                             deadline_s=2.0)
        self.assertEqual(body["revision"]["commit"], self.fixture.target)


class ActivationFlowTest(_Case):
    """The activate command: journaled exact-revision activation under the lock."""

    def _run(self):
        return safe_update.cmd_activate(TXN, str(self.repo),
                                        inspect=self.fake_inspect,
                                        restart=self.fake_restart, deadline_s=1.0)

    def test_exact_revision_activation_is_verified_and_finishes(self):
        self._patch(safe_update, "_authenticated_get", self._fake_authenticated_get)
        old_inventory = self._tree_snapshot(self.fixture.stage)
        rc = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(_run_git("-C", str(self.repo), "rev-parse", "HEAD"), self.fixture.target,
                         "the checkout runs the exact staged revision now")
        rollback = _run_git("-C", str(self.repo), "rev-parse",
                            "refs/agent-relay/update-rollback/%s" % TXN)
        self.assertEqual(rollback, self.fixture.old, "the previous object stays available")
        self.assertIsNone(update_transaction.read_journal(state_dir=self.state),
                          "a proven activation expunges its own record")
        self.assertIsNone(update_transaction.read_barrier(state_dir=self.state))
        self.assertIsNone(update_transaction.admission_blocker(state_dir=self.state))
        kinds = [call[0] for call in self.calls]
        self.assertLess(kinds.index("restart"), kinds.index("get"),
                        "the relay is verified only after the boundary acted")
        self.assertEqual(kinds[-1], "get", "the last probe is the authenticated barrier")
        self.assertEqual(self.calls[-1][1], safe_update.BARRIER_PATH)
        for argv in self.restart_commands:
            self.assertNotIn("git pull", argv)
            self.assertNotIn("/usr/bin/sudo", argv)
            self.assertEqual(Path(argv[0]).name, "fake-manager")
        self.assertEqual(self._tree_snapshot(self.fixture.stage), old_inventory,
                         "the stage is untouched by activation")

    def _tree_snapshot(self, root):
        snapshot = {}
        for dirpath, dirnames, filenames in os.walk(str(root)):
            dirnames[:] = [name for name in dirnames if name != ".git"]
            for name in dirnames + filenames:
                path = Path(dirpath) / name
                payload = b"" if path.is_dir() else path.read_bytes()
                snapshot[path.relative_to(root).as_posix()] = (
                    stat.S_IFMT(path.lstat().st_mode), len(payload),
                    hashlib.sha256(payload).hexdigest())
        return snapshot

    def test_source_and_identity_mutation_is_refused_before_any_effect(self):
        self._patch(safe_update, "_authenticated_get", self._fake_authenticated_get)
        (self.repo / "README.md").write_text("# dirty\n")
        with self.assertRaises(identity.UpdateError) as caught:
            self._run()
        self.assertIn("uncommitted or untracked", str(caught.exception),
                      "the dirty checkout is refused before any effect")
        self.assertEqual(_run_git("-C", str(self.repo), "rev-parse", "HEAD"), self.fixture.old)
        record = update_transaction.recover_transaction(state_dir=self.state)
        self.assertEqual(record["phase"], "prepared", "no effect happened for a dirty tree")
        self.assertIsNotNone(update_transaction.read_barrier(state_dir=self.state))
        self.assertEqual(self.restart_commands, [])
        _run_git("-C", str(self.repo), "checkout", "-q", "--", "README.md")
        self.quiesced = False
        # the fake probe would otherwise rename the activated commit only after
        # a restart; the hold it names is exactly what is refused first
        with self.assertRaises(identity.UpdateError) as caught:
            self._run()
        self.assertIn("quiesc", str(caught.exception))
        self.assertEqual(update_transaction.recover_transaction(
            state_dir=self.state)["phase"], "prepared")

    def test_a_failed_service_restart_leaves_a_recoverable_transaction(self):
        def failing_restart(service):
            self.calls.append(("restart", service["kind"]))
            raise identity.UpdateError("the user launchd refused the restart")
        before = self._external_snapshot()
        head_before = _run_git("-C", str(self.repo), "rev-parse", "HEAD")
        with self.assertRaises(identity.UpdateError) as caught:
            safe_update.cmd_activate(TXN, str(self.repo), inspect=self.fake_inspect,
                                     restart=failing_restart, deadline_s=1.0)
        self.assertIn("recovery", str(caught.exception))
        record = update_transaction.recover_transaction(state_dir=self.state)
        self.assertEqual(record["phase"], "recovery_required",
                         "the interrupted activation is durably recorded and recoverable")
        self.assertIsNotNone(update_transaction.read_barrier(state_dir=self.state),
                             "the barrier still denies admission after the interruption")
        self.assertEqual(self._external_snapshot(), before,
                         "external config, token state and service definition are unchanged")

    def test_unknown_health_leaves_a_durable_recovery_required_state(self):
        self._patch(safe_update, "_authenticated_get", lambda _b, _t, _p: {"ok": True})
        before = self._external_snapshot()
        with self.assertRaises(identity.UpdateError) as caught:
            self._run()
        self.assertIn("recovery", str(caught.exception))
        record = update_transaction.recover_transaction(state_dir=self.state)
        self.assertEqual(record["phase"], "recovery_required")
        self.assertIsNotNone(update_transaction.read_barrier(state_dir=self.state),
                             "the barrier stays retained")
        refused = update_transaction.admission_blocker(state_dir=self.state)
        self.assertIsNotNone(refused, "the barrier fails every admission closed")
        self.assertEqual(self._external_snapshot(), before,
                         "external config, token state and service definition are unchanged")

    def _external_snapshot(self):
        point = self.state / "consent.json"
        service = self.fixture.home / "Library" / "LaunchAgents" / "com.agent-relay.plist"
        self.fixture.write_plist() if not service.exists() else None
        return {"consent": hashlib.sha256(point.read_bytes()).hexdigest(),
                "device": (self.state / "device-id").read_bytes(),
                "service": hashlib.sha256(
                    service.read_bytes()).hexdigest() if service.exists() else b""}

    def test_recover_abort_refuses_a_recovery_required_transaction(self):
        self._patch(safe_update, "_authenticated_get", lambda _b, _t, _p: {"ok": True})
        with self.assertRaises(identity.UpdateError):
            self._run()
        with self.assertRaises(identity.UpdateError) as caught:
            safe_update.cmd_recover_abort(TXN)
        self.assertIn("manual recovery", str(caught.exception))
        record = update_transaction.recover_transaction(state_dir=self.state)
        self.assertEqual(record["phase"], "recovery_required")


if __name__ == "__main__":
    unittest.main()
