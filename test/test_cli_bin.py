"""The agent-relay command on PATH and what it reports."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = sys.executable
INSTALL_SERVICE = os.path.join(ROOT, "scripts", "install-service.sh")
CLI = os.path.join(ROOT, "src", "cli.py")


def _fake_checkout(checkout):
    """A checkout whose src/ is complete enough for the command to run."""
    shutil.copytree(os.path.join(ROOT, "src"), os.path.join(checkout, "src"))


def _installer_env(tmp, checkout):
    """Stub uname and launchctl; HOME is tmp so the command lands there."""
    stubs = os.path.join(tmp, "stubs")
    os.makedirs(stubs)
    for name, body in (("uname", "echo Darwin\n"),
                       ("launchctl", "exit 0\n")):
        stub = os.path.join(stubs, name)
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write(f"#!/bin/sh\n{body}")
        os.chmod(stub, 0o755)
    return {**os.environ, "HOME": tmp, "AGENT_RELAY_DIR": checkout,
            "AGENT_RELAY_PYTHON": PYTHON,
            "PATH": f"{stubs}:{os.environ['PATH']}"}


class InstallerPutsTheCommandOnPathTests(unittest.TestCase):
    """scripts/install-service.sh installs a resolvable agent-relay command."""

    def test_the_installer_puts_the_command_on_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = os.path.join(tmp, "checkout")
            _fake_checkout(checkout)
            env = _installer_env(tmp, checkout)
            result = subprocess.run(
                ["bash", INSTALL_SERVICE, "--no-update-timer"], env=env,
                cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

            command = os.path.join(tmp, ".local", "bin", "agent-relay")
            self.assertTrue(os.path.exists(command))
            mode = os.stat(command).st_mode
            self.assertTrue(mode & stat.S_IXUSR)

            path_env = {**env, "PATH": f"{os.path.dirname(command)}:{env['PATH']}",
                        "HOME": tmp}
            result = subprocess.run(
                ["bash", "-c", "command -v agent-relay && agent-relay info"],
                env=path_env, cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines()[0], command)
            self.assertIn(f"checkout: {checkout}", result.stdout)


class InfoTests(unittest.TestCase):
    """agent-relay info names the four facts in one call."""

    def test_info_names_the_four_facts(self):
        token_file = os.path.join(ROOT, "test", "fixtures")
        env = {**os.environ,
               "AGENT_RELAY_URL": "http://127.0.0.1:49999",
               "AGENT_RELAY_TOKEN_FILE": token_file}
        result = subprocess.run(
            [PYTHON, CLI, "info"], env=env, cwd=ROOT,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "relay: http://127.0.0.1:49999",
            "artifact: https://github.com/Ami-Khokhar/agent-relay/releases",
            f"token: {token_file}",
            f"checkout: {ROOT}",
        ])


if __name__ == "__main__":
    unittest.main()
