"""Behaviour of scripts/update.sh and the health endpoint it relies on."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from urllib.error import URLError
from urllib import request as urlrequest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from relay_helpers import (ROOT, PYTHON, SERVER, McpClient, Relay, send_json,
                           start_http_server, stop_http_server)
import server

UPDATE = os.path.join(ROOT, "scripts", "update.sh")
INSTALL_SERVICE = os.path.join(ROOT, "scripts", "install-service.sh")
UNKNOWN_REVISION = {"commit": "unknown", "short": "unknown", "date": "unknown"}
REVISION_DATE = "2026-09-23T10:00:00+05:30"


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
            relay = Relay({"id": "echo", "command": PYTHON,
                           "args": ["-c", "print(1)"]})
            try:
                port = relay.base.rsplit(":", 1)[1]
                env = self._stub_env(checkout, port)
                result = self._run(env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("already up to date", result.stdout)
                with open(env["AGENT_RELAY_RESTART_LOG"], encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "", "the service was restarted")
            finally:
                relay.close()

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
                self.assertIn("relay healthy on port", result.stdout)
                with open(env["AGENT_RELAY_RESTART_LOG"], encoding="utf-8") as handle:
                    self.assertIn("kickstart", handle.read())
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
                with open(env["AGENT_RELAY_RESTART_LOG"], encoding="utf-8") as handle:
                    self.assertIn("kickstart", handle.read())
            finally:
                relay.close()

    def test_no_restart_defers_without_waiting_for_idle(self):
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
                relay.submit(agentId="slow", input="long work")

                result = self._run(env, "--no-restart")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--no-restart: restart deferred", result.stdout)
                pending = os.path.join(checkout, ".git", "agent-relay-pending-restart")
                self.assertTrue(os.path.exists(pending))
                with open(env["AGENT_RELAY_RESTART_LOG"], encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "")
            finally:
                relay.close()

    def test_unknown_health_escalates_after_one_deferral(self):
        """A pre-feature relay that cannot report counts must not defer forever:
        the first run records the deferral, the second run restarts it."""
        with tempfile.TemporaryDirectory() as tmp:
            seed = init_repo(os.path.join(tmp, "seed"))
            checkout = clone_repo(seed, os.path.join(tmp, "checkout"))
            # A 'relay' whose /healthz answers ok but without the new counts.
            server, port = start_http_server(lambda request: send_json(
                request, 200, {"ok": True, "agents": 1, "tasks": 0}))
            try:
                env = self._stub_env(checkout, str(port))
                advance_repo(seed, "c-new")
                subprocess.run(["git", "fetch", "origin"], cwd=checkout, check=True,
                               capture_output=True, text=True)

                result = self._run(env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("skipping restart", result.stdout)
                with open(env["AGENT_RELAY_RESTART_LOG"], encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "")

                result = self._run(env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("completing a restart deferred earlier", result.stdout)
                self.assertIn("relay healthy on port", result.stdout)
                with open(env["AGENT_RELAY_RESTART_LOG"], encoding="utf-8") as handle:
                    self.assertIn("kickstart", handle.read())
            finally:
                stop_http_server(server)

    def test_busy_relay_with_a_pending_restart_is_deferred_again(self):
        """Escalation only overrides an unreadable verdict: a relay that visibly
        reports busy tasks keeps being deferred."""
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

                relay.submit(agentId="slow", input="long work")
                result = self._run(env)
                self.assertIn("skipping restart", result.stdout)
                result = self._run(env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("skipping restart", result.stdout)
                with open(env["AGENT_RELAY_RESTART_LOG"], encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "")
            finally:
                relay.close()

    def test_no_restart_never_restarts_even_when_idle(self):
        """--no-restart must defer even on a fresh update with an idle relay."""
        with tempfile.TemporaryDirectory() as tmp:
            seed = init_repo(os.path.join(tmp, "seed"))
            checkout = clone_repo(seed, os.path.join(tmp, "checkout"))
            relay = Relay({"id": "echo", "command": PYTHON,
                           "args": ["-c", "print(1)"]})
            try:
                port = relay.base.rsplit(":", 1)[1]
                env = self._stub_env(checkout, port)
                advance_repo(seed, "c-new")
                subprocess.run(["git", "fetch", "origin"], cwd=checkout, check=True,
                               capture_output=True, text=True)

                result = self._run(env, "--no-restart")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--no-restart: restart deferred", result.stdout)
                with open(env["AGENT_RELAY_RESTART_LOG"], encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "")

                # A later plain run completes it.
                result = self._run(env)
                self.assertIn("completing a restart deferred earlier", result.stdout)
                with open(env["AGENT_RELAY_RESTART_LOG"], encoding="utf-8") as handle:
                    self.assertIn("kickstart", handle.read())
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
                with open(env["AGENT_RELAY_RESTART_LOG"], encoding="utf-8") as handle:
                    self.assertIn("kickstart", handle.read())
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
    def _stub_env(checkout, port, service_restart_log=None):
        stubs = tempfile.mkdtemp(prefix="a2a-stubs-")
        if service_restart_log is None:
            service_restart_log = os.path.join(stubs, "restarts.log")
        open(service_restart_log, "w").close()
        for name, body in (
                ("uname", "echo Darwin\n"),
                ("launchctl",
                 f"echo \"stubbed restart: $*\" >> {service_restart_log}\n"),
        ):
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
                "PATH": f"{stubs}:{os.environ['PATH']}", "A2A_RELAY_PORT": port,
                "AGENT_RELAY_RESTART_LOG": service_restart_log}

    @staticmethod
    def _run(env, *args):
        return subprocess.run(["bash", UPDATE, *args], env=env, cwd=ROOT,
                              capture_output=True, text=True, timeout=60)


class UpdateTimerTests(unittest.TestCase):
    """install-service.sh generates the update timer's launchd plist on Darwin
    (stubbed uname) unless --no-update-timer is given, and --uninstall removes
    both files."""

    @staticmethod
    def _fake_checkout(checkout):
        """install-service.sh only checks the layout, not a real checkout."""
        os.makedirs(os.path.join(checkout, "src"), exist_ok=True)
        open(os.path.join(checkout, "src", "server.py"), "w").close()

    def _env(self, tmp, checkout, system="Darwin"):
        """Stub uname, launchctl and systemctl; each service-manager call is
        appended to <tmp>/service-manager.log as '<tool> <arguments>'."""
        stubs = os.path.join(tmp, "stubs")
        os.makedirs(stubs)
        log = os.path.join(tmp, "service-manager.log")
        for name, body in (("uname", f"echo {system}\n"),
                           ("launchctl", f'echo "launchctl $*" >> "{log}"\n'),
                           ("systemctl", f'echo "systemctl $*" >> "{log}"\n')):
            stub = os.path.join(stubs, name)
            with open(stub, "w", encoding="utf-8") as handle:
                handle.write(f"#!/bin/sh\n{body}")
            os.chmod(stub, 0o755)
        return {**os.environ, "HOME": tmp, "AGENT_RELAY_DIR": checkout,
                "PATH": f"{stubs}:{os.environ['PATH']}",
                "AGENT_RELAY_PYTHON": PYTHON}

    @staticmethod
    def _take_log(tmp):
        """Return the service-manager calls since the last call, then clear the log."""
        log = os.path.join(tmp, "service-manager.log")
        if not os.path.exists(log):
            return []
        with open(log, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        os.remove(log)
        return lines

    def test_with_update_timer_installs_and_uninstall_removes_both(self):
        install_service = os.path.join(ROOT, "scripts", "install-service.sh")
        with tempfile.TemporaryDirectory() as tmp:
            seed = init_repo(os.path.join(tmp, "seed"))
            checkout = clone_repo(seed, os.path.join(tmp, "checkout"))
            env = self._env(tmp, checkout)
            self._fake_checkout(checkout)

            result = subprocess.run(
                ["bash", install_service, "--with-update-timer"], env=env,
                cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("unknown argument", result.stderr)
            service_plist = os.path.join(
                tmp, "Library", "LaunchAgents", "com.agent-relay.plist")
            update_plist = os.path.join(
                tmp, "Library", "LaunchAgents", "com.agent-relay.update.plist")
            self.assertTrue(os.path.exists(service_plist))
            self.assertTrue(os.path.exists(update_plist))
            with open(update_plist, encoding="utf-8") as handle:
                body = handle.read()
            self.assertIn("<key>StartInterval</key><integer>21600</integer>", body)
            self.assertIn(f"<string>{checkout}/scripts/update.sh</string>", body)
            self.assertIn("<key>A2A_RELAY_PORT</key>", body)
            self.assertIn("<key>AGENT_RELAY_DIR</key>", body)

            result = subprocess.run(
                ["bash", install_service, "--uninstall"], env=env,
                cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(os.path.exists(service_plist))
            self.assertFalse(os.path.exists(update_plist))

    def test_interval_env_override_lands_in_the_plist(self):
        install_service = os.path.join(ROOT, "scripts", "install-service.sh")
        with tempfile.TemporaryDirectory() as tmp:
            seed = init_repo(os.path.join(tmp, "seed"))
            checkout = clone_repo(seed, os.path.join(tmp, "checkout"))
            env = {**self._env(tmp, checkout), "AGENT_RELAY_UPDATE_INTERVAL_SECS": "60"}
            self._fake_checkout(checkout)
            result = subprocess.run(
                ["bash", install_service, "--with-update-timer"], env=env,
                cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            with open(os.path.join(
                    tmp, "Library", "LaunchAgents",
                    "com.agent-relay.update.plist"), encoding="utf-8") as handle:
                self.assertIn("<key>StartInterval</key><integer>60</integer>",
                              handle.read())

    def _install(self, env, *args):
        return subprocess.run(["bash", INSTALL_SERVICE, *args], env=env, cwd=ROOT,
                              capture_output=True, text=True, timeout=30)

    def test_plain_install_also_installs_the_update_timer(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = init_repo(os.path.join(tmp, "seed"))
            checkout = clone_repo(seed, os.path.join(tmp, "checkout"))
            env = self._env(tmp, checkout)
            self._fake_checkout(checkout)
            agents = os.path.join(tmp, "Library", "LaunchAgents")

            result = self._install(env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(os.path.exists(os.path.join(agents, "com.agent-relay.plist")))
            self.assertTrue(os.path.exists(os.path.join(agents, "com.agent-relay.update.plist")))
            self.assertIn("update timer installed", result.stdout)

            result = self._install(env, "--uninstall")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(os.listdir(agents), [])

    def test_no_update_timer_installs_only_the_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = init_repo(os.path.join(tmp, "seed"))
            checkout = clone_repo(seed, os.path.join(tmp, "checkout"))
            env = self._env(tmp, checkout)
            self._fake_checkout(checkout)
            agents = os.path.join(tmp, "Library", "LaunchAgents")

            result = self._install(env, "--no-update-timer")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("unknown argument", result.stderr)
            self.assertEqual(os.listdir(agents), ["com.agent-relay.plist"])
            self.assertIn("no update timer installed", result.stdout)
            self.assertNotIn("removed", result.stdout)
            # Nothing existed, so nothing is unloaded for the update timer.
            self.assertFalse([line for line in self._take_log(tmp) if "update" in line])

    def test_no_update_timer_removes_an_update_timer_from_an_earlier_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = init_repo(os.path.join(tmp, "seed"))
            checkout = clone_repo(seed, os.path.join(tmp, "checkout"))
            env = self._env(tmp, checkout)
            self._fake_checkout(checkout)
            agents = os.path.join(tmp, "Library", "LaunchAgents")
            service_plist = os.path.join(agents, "com.agent-relay.plist")
            update_plist = os.path.join(agents, "com.agent-relay.update.plist")

            self.assertEqual(self._install(env).returncode, 0)
            self.assertTrue(os.path.exists(update_plist))
            self._take_log(tmp)

            result = self._install(env, "--no-update-timer")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(os.path.exists(update_plist))
            self.assertTrue(os.path.exists(service_plist))
            self.assertEqual(os.listdir(agents), ["com.agent-relay.plist"])
            self.assertIn(f"launchctl unload {update_plist}", self._take_log(tmp))
            self.assertIn("removed the existing update timer", result.stdout)
            self.assertNotIn("no update timer installed", result.stdout)

    def test_no_update_timer_removes_the_systemd_update_units_but_keeps_the_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = init_repo(os.path.join(tmp, "seed"))
            checkout = clone_repo(seed, os.path.join(tmp, "checkout"))
            env = self._env(tmp, checkout, system="Linux")
            self._fake_checkout(checkout)
            units = os.path.join(tmp, ".config", "systemd", "user")

            self.assertEqual(self._install(env).returncode, 0)
            self.assertEqual(sorted(os.listdir(units)),
                             ["agent-relay-update.service", "agent-relay-update.timer",
                              "agent-relay.service"])
            self._take_log(tmp)

            result = self._install(env, "--no-update-timer")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(os.listdir(units), ["agent-relay.service"])
            self.assertIn("systemctl --user disable --now agent-relay-update.timer",
                          self._take_log(tmp))
            self.assertIn("removed the existing update timer", result.stdout)
            self.assertNotIn("no update timer installed", result.stdout)

    def test_no_update_timer_on_linux_without_an_earlier_timer_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = init_repo(os.path.join(tmp, "seed"))
            checkout = clone_repo(seed, os.path.join(tmp, "checkout"))
            env = self._env(tmp, checkout, system="Linux")
            self._fake_checkout(checkout)
            units = os.path.join(tmp, ".config", "systemd", "user")

            result = self._install(env, "--no-update-timer")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(os.listdir(units), ["agent-relay.service"])
            self.assertIn("no update timer installed", result.stdout)
            self.assertNotIn("removed", result.stdout)
            self.assertFalse([line for line in self._take_log(tmp)
                              if "update.timer" in line])

    def test_update_timer_is_skipped_with_a_warning_outside_a_git_checkout(self):
        """update.sh cannot update a directory that is not a git clone, so a
        timer for it would only fail every interval."""
        with tempfile.TemporaryDirectory() as tmp:
            checkout = os.path.join(tmp, "copy")
            env = self._env(tmp, checkout)
            self._fake_checkout(checkout)
            agents = os.path.join(tmp, "Library", "LaunchAgents")

            result = self._install(env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(os.listdir(agents), ["com.agent-relay.plist"])
            self.assertIn("not a git checkout", result.stderr)


class DeployedRevisionTests(unittest.TestCase):
    """The relay reports the commit of the checkout it runs from, so a stale
    install is visible without reading git."""

    @staticmethod
    def _checkout(tmp):
        """A git checkout holding a copy of server.py, with a fixed commit date."""
        repo = init_repo(os.path.join(tmp, "checkout"), commits=0)
        os.makedirs(os.path.join(repo, "src"))
        shutil.copy(SERVER, os.path.join(repo, "src", "server.py"))
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
               "GIT_COMMITTER_DATE": REVISION_DATE}
        for args in (("add", "."), ("commit", "-qm", "dated")):
            subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                           capture_output=True, text=True)
        return repo

    def test_reads_the_commit_and_its_date_from_a_git_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._checkout(tmp)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                                  capture_output=True, text=True).stdout.strip()
            revision = server.deployed_revision(repo)
            self.assertEqual(revision["commit"], head)
            self.assertEqual(len(head), 40)
            self.assertGreaterEqual(len(revision["short"]), 7)
            self.assertTrue(head.startswith(revision["short"]))
            self.assertEqual(revision["date"], REVISION_DATE)

    def test_reports_unknown_outside_a_git_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(server.deployed_revision(tmp), UNKNOWN_REVISION)
            # A folder inside someone else's repository is not itself a checkout.
            repo = self._checkout(tmp)
            self.assertEqual(server.deployed_revision(os.path.join(repo, "src")),
                             UNKNOWN_REVISION)

    def test_reports_unknown_when_git_is_missing_hangs_or_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._checkout(tmp)
            for failure in (FileNotFoundError("git"), subprocess.TimeoutExpired("git", 5)):
                with mock.patch.object(server.subprocess, "run", side_effect=failure):
                    self.assertEqual(server.deployed_revision(repo), UNKNOWN_REVISION)
            # A repository with no commit makes git exit non-zero.
            empty = init_repo(os.path.join(tmp, "empty"), commits=0)
            self.assertEqual(server.deployed_revision(empty), UNKNOWN_REVISION)

    def test_reports_unknown_when_git_exits_non_zero_even_with_plausible_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._checkout(tmp)
            stubs = os.path.join(tmp, "stubs")
            os.makedirs(stubs)
            stub = os.path.join(stubs, "git")
            with open(stub, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/sh\necho 0123456789abcdef0123456789abcdef01234567\n"
                             "echo 0123456\necho 2026-09-23T10:00:00+05:30\nexit 1\n")
            os.chmod(stub, 0o755)
            with mock.patch.dict(os.environ, {"PATH": f"{stubs}:{os.environ['PATH']}"}):
                self.assertEqual(server.deployed_revision(repo), UNKNOWN_REVISION)

    def test_a_running_relay_and_its_mcp_list_agents_report_the_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._checkout(tmp)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                                  capture_output=True, text=True).stdout.strip()
            relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print(1)"]},
                          server=os.path.join(repo, "src", "server.py"))
            client = McpClient(relay.base)
            try:
                health = relay.request("GET", "/healthz", token=None)[1]
                self.assertEqual(health["revision"]["commit"], head)
                self.assertEqual(health["revision"]["date"], REVISION_DATE)
                listing = relay.request("GET", "/v1/agents")[1]
                self.assertEqual(listing["revision"], health["revision"])
                listed = client.call_tool("list_agents", {})["structuredContent"]
                self.assertEqual(listed["revision"], health["revision"])
            finally:
                client.close()
                relay.close()

    def test_a_relay_outside_a_git_checkout_still_starts_and_reports_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "src"))
            shutil.copy(SERVER, os.path.join(tmp, "src", "server.py"))
            relay = Relay({"id": "echo", "command": PYTHON, "args": ["-c", "print(1)"]},
                          server=os.path.join(tmp, "src", "server.py"))
            try:
                health = relay.request("GET", "/healthz", token=None)[1]
                self.assertEqual(health["revision"], UNKNOWN_REVISION)
                self.assertEqual(relay.request("GET", "/v1/agents")[1]["revision"],
                                 UNKNOWN_REVISION)
            finally:
                relay.close()


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
