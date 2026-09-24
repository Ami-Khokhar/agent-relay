"""Behaviour of scripts/update.sh and the health endpoint it relies on."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from urllib.error import URLError
from urllib import request as urlrequest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_helpers import ROOT, PYTHON, Relay

UPDATE = os.path.join(ROOT, "scripts", "update.sh")


def run_update(env_target, *args):
    env = {
        **os.environ,
        "AGENT_RELAY_DIR": env_target,
        "PATH": os.environ["PATH"],
    }
    return subprocess.run(["bash", UPDATE, *args], env=env, cwd=ROOT,
                          capture_output=True, text=True, timeout=60)


def init_repo(path, commits=2):
    """Create a tiny git repo that stands in for the installed checkout."""
    os.makedirs(path, exist_ok=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    def git(*args):
        subprocess.run(["git", *args], cwd=path, env=env, check=True,
                       capture_output=True, text=True)
    git("init", "-q", "-b", "main")
    for index in range(commits):
        with open(os.path.join(path, "f.txt"), "a", encoding="utf-8") as handle:
            handle.write(f"{index}\n")
        git("add", ".")
        git("commit", "-qm", f"c{index}")
    return path


def clone_repo(seed, path):
    """Clone the seed as origin/main would be, so git pull --ff-only works."""
    subprocess.run(["git", "clone", "-q", "--origin", "origin", seed, path],
                   check=True, capture_output=True, text=True)
    return path


def advance_repo(path, marker):
    """Add a commit on the checkout's main branch (as the remote would have)."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    with open(os.path.join(path, "f.txt"), "a", encoding="utf-8") as handle:
        handle.write(f"{marker}\n")
    for arg in (("add", "."), ("commit", "-qm", marker)):
        subprocess.run(["git", *arg], cwd=path, env=env, check=True,
                       capture_output=True, text=True)


class UpdateScriptTests(unittest.TestCase):
    def test_reports_unknown_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_update(tmp, "--bogus")
            self.assertEqual(result.returncode, 2)
            self.assertIn("usage", result.stderr)

    def test_no_change_makes_no_restart_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = init_repo(os.path.join(tmp, "seed"))
            checkout = clone_repo(seed, os.path.join(tmp, "checkout"))
            result = run_update(checkout)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already up to date", result.stdout)
            self.assertNotIn("restarting", result.stdout)

    def test_new_commit_restarts_and_verifies_health(self):
        """A real relay runs on a helper-chosen port; uname/launchctl are stubbed
        through PATH so the restart branch runs without touching launchd."""
        with tempfile.TemporaryDirectory() as tmp:
            seed = init_repo(os.path.join(tmp, "seed"))
            checkout = clone_repo(seed, os.path.join(tmp, "checkout"))
            relay = Relay({"id": "echo", "command": PYTHON,
                           "args": ["-c", "print(1)"]})
            try:
                port = relay.base.rsplit(":", 1)[1]
                env = self._stub_env(checkout, port)
                # First run: up to date, no restart.
                result = self._run(env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("already up to date", result.stdout)

                # The remote gains a commit; the checkout fast-forwards to it.
                advance_repo(seed, "c-new")
                subprocess.run(["git", "fetch", "origin"], cwd=checkout, check=True,
                               capture_output=True, text=True)
                result = self._run(env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("updated:", result.stdout)
                self.assertIn("stubbed restart", result.stdout)
                self.assertIn("relay healthy on port", result.stdout)
            finally:
                relay.close()

    def test_new_commit_with_busy_relay_skips_restart_unless_forced(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = init_repo(os.path.join(tmp, "seed"))
            checkout = clone_repo(seed, os.path.join(tmp, "checkout"))
            relay = Relay({"id": "slow", "command": PYTHON,
                           "args": ["-c", "import time; time.sleep(30)"]})
            try:
                port = relay.base.rsplit(":", 1)[1]
                env = self._stub_env(checkout, port)
                advance_repo(seed, "c-new")
                subprocess.run(["git", "fetch", "origin"], cwd=checkout, check=True,
                               capture_output=True, text=True)

                _, submitted = relay.submit(agentId="slow", input="long work")
                self.assertTrue(submitted["id"])

                result = self._run(env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("skipping restart", result.stdout)

                # Another commit arrives while the task is still running.
                advance_repo(seed, "c-newer")
                subprocess.run(["git", "fetch", "origin"], cwd=checkout, check=True,
                               capture_output=True, text=True)
                result = self._run(env, "--force")
                self.assertIn("stubbed restart", result.stdout)
            finally:
                relay.close()

    def test_deferred_restart_completes_on_a_later_idle_run(self):
        """A restart skipped while busy must not be lost: the next run, still
        up to date, completes it once the relay is idle again."""
        with tempfile.TemporaryDirectory() as tmp:
            seed = init_repo(os.path.join(tmp, "seed"))
            checkout = clone_repo(seed, os.path.join(tmp, "checkout"))
            relay = Relay({"id": "slow", "command": PYTHON,
                           "args": ["-c", "import time; time.sleep(2)"]})
            try:
                port = relay.base.rsplit(":", 1)[1]
                env = self._stub_env(checkout, port)
                advance_repo(seed, "c-new")
                subprocess.run(["git", "fetch", "origin"], cwd=checkout, check=True,
                               capture_output=True, text=True)

                _, submitted = relay.submit(agentId="slow", input="work in flight")
                result = self._run(env)
                self.assertIn("skipping restart", result.stdout)
                pending = os.path.join(checkout, ".git", "agent-relay-pending-restart")
                self.assertTrue(os.path.exists(pending))

                relay.wait_task(submitted["id"])
                result = self._run(env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("completing a restart deferred earlier", result.stdout)
                self.assertIn("stubbed restart", result.stdout)
                self.assertFalse(os.path.exists(pending))
            finally:
                relay.close()

    def test_bootstrapping_without_the_setup_script_fails_with_a_clear_error(self):
        """update.sh resolves the bundled setup script relative to itself; run it
        from a bare scripts/ dir so the guard (not a network clone) is tested."""
        with tempfile.TemporaryDirectory() as tmp:
            scripts = os.path.join(tmp, "scripts")
            os.makedirs(scripts)
            shutil.copy(UPDATE, os.path.join(scripts, "update.sh"))
            target = os.path.join(tmp, "install")
            os.makedirs(target)
            env = {**os.environ, "AGENT_RELAY_DIR": target}
            result = subprocess.run(["bash", os.path.join(scripts, "update.sh")],
                                    env=env, cwd=ROOT, capture_output=True, text=True,
                                    timeout=30)
            self.assertEqual(result.returncode, 1)
            self.assertIn("setup script not found", result.stderr)

    @staticmethod
    def _stub_env(checkout, port):
        stubs = tempfile.mkdtemp(prefix="a2a-stubs-")
        for name, body in (("uname", "echo Darwin\n"),
                           ("launchctl", "echo \"stubbed restart: $*\"\n")):
            stub = os.path.join(stubs, name)
            with open(stub, "w", encoding="utf-8") as handle:
                handle.write(f"#!/bin/sh\n{body}")
            os.chmod(stub, 0o755)
        # A fake HOME with a service plist so the Darwin restart branch runs.
        home = tempfile.mkdtemp(prefix="a2a-home-")
        plist_dir = os.path.join(home, "Library", "LaunchAgents")
        os.makedirs(plist_dir)
        open(os.path.join(plist_dir, "com.agent-relay.plist"), "w").close()
        return {**os.environ, "AGENT_RELAY_DIR": checkout, "HOME": home,
                "PATH": f"{stubs}:{os.environ['PATH']}", "A2A_RELAY_PORT": port}

    @staticmethod
    def _run(env, *args):
        return subprocess.run(["bash", UPDATE, *args], env=env, cwd=ROOT,
                              capture_output=True, text=True, timeout=60)


class HealthzCountsTests(unittest.TestCase):
    def test_healthz_reports_active_and_queued(self):
        relay = Relay({"id": "slow", "command": PYTHON,
                       "args": ["-c", "import time; time.sleep(30)"]},
                      env={"A2A_RELAY_MAX_ACTIVE": "1"})
        try:
            relay.submit(agentId="slow", input="one")
            relay.submit(agentId="slow", input="two")
            with urlrequest.urlopen(f"{relay.base}/healthz", timeout=5) as response:
                health = json.loads(response.read().decode("utf-8"))
            self.assertEqual(health["active"], 1)
            self.assertEqual(health["queued"], 1)
        finally:
            relay.close()


if __name__ == "__main__":
    unittest.main()
