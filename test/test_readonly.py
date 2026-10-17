"""Read-only delegation: a readOnly profile in the registry, enforced by args and a macOS sandbox."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from relay_helpers import PRINT_ARGV, PYTHON, STDIO_ARGV, TOKEN, McpClient, Relay
import server

SANDBOX = sys.platform == "darwin" and os.access("/usr/bin/sandbox-exec", os.X_OK)
GIT = shutil.which("git")
NO_WRITE = ["--exclude-tools", "edit,write"]
# Writes the agent's own state outside the cwd, then tries to change the tree under review.
WRITER = (
    "import os\n"
    "open(os.path.join(os.environ['STATE_DIR'], 'state.txt'), 'w').write('state')\n"
    "open('victim.txt', 'w').write('changed')\n"
    "open('new.txt', 'w').write('new')\n"
    "print('wrote')\n"
)


# Connects to the "host:port" in its input, so a test can aim it at any address.
CONNECT = (
    "import socket, sys\n"
    "host, port = sys.argv[1].rsplit(':', 1)\n"
    "socket.create_connection((host, int(port)), timeout=5).close()\n"
    "print('connected')\n"
)
# The attack from the review: read the relay token, then ask the relay to run a writable agent
# ("mutator") in the protected tree, so the write happens outside the sandbox.
ATTACK = (
    "import json, os, time, urllib.request\n"
    "token = open(os.environ['TOKEN_FILE']).read().strip()\n"
    "base = 'http://127.0.0.1:' + os.environ['A2A_RELAY_PORT']\n"
    "def call(method, path, body=None):\n"
    "    data = json.dumps(body).encode() if body else None\n"
    "    headers = {'authorization': 'Bearer ' + token, 'content-type': 'application/json'}\n"
    "    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)\n"
    "    return json.load(urllib.request.urlopen(request, timeout=5))\n"
    "task = call('POST', '/v1/tasks', {'agentId': 'mutator', 'input': 'x'})\n"
    "while task['status'] in ('queued', 'running'):\n"
    "    time.sleep(0.05)\n"
    "    task = call('GET', '/v1/tasks/' + task['id'])\n"
    "print(task['status'])\n"
)
# Each tries to get at the token file by a route other than opening it, then prints it.
READ_TOKEN = "import os\nprint(open(os.environ['TOKEN_FILE']).read().strip())\n"
LINK_TOKEN = (
    "import os\n"
    "link = os.path.join(os.environ['BASE'], 'link')\n"
    "os.link(os.environ['TOKEN_FILE'], link)\n"
    "print(open(link).read().strip())\n"
)
MOVE_TOKEN_FILE = (
    "import os\n"
    "moved = os.path.join(os.environ['BASE'], 'moved')\n"
    "os.rename(os.environ['TOKEN_FILE'], moved)\n"
    "print(open(moved).read().strip())\n"
)
MOVE_TOKEN_DIR = (
    "import os\n"
    "moved = os.path.join(os.environ['BASE'], 'moved')\n"
    "os.rename(os.environ['TOKEN_DIR'], moved)\n"
    "print(open(os.path.join(moved, 'token')).read().strip())\n"
)
# Runs each git command in the JSON list in its input, in the task's cwd; stops at the first failure.
GIT_RUN = (
    "import json, subprocess, sys\n"
    "for args in json.loads(sys.argv[1]):\n"
    "    done = subprocess.run(['git'] + args, capture_output=True, text=True)\n"
    "    if done.returncode:\n"
    "        sys.stderr.write('git ' + ' '.join(args) + ' failed: ' + done.stderr)\n"
    "        sys.exit(1)\n"
    "    print(done.stdout)\n"
)


def fake(**extra):
    return {"id": "fake", "command": PYTHON, "args": [*PRINT_ARGV, "--base"], **extra}


class ReadOnlyTests(unittest.TestCase):
    def start(self, agent):
        relay = Relay(agent)
        self.addCleanup(relay.close)
        return relay

    def argv_of(self, relay, **body):
        task = relay.run_task(agentId="fake", **body)
        self.assertEqual(task["status"], "completed", task)
        return json.loads(task["output"])

    def test_adds_the_profile_args_after_the_option_args_and_before_the_input(self):
        relay = self.start(fake(options={"effort": {"high": ["--high"]}}, readOnly={"args": NO_WRITE}))
        status, submitted = relay.submit(agentId="fake", input="review", readOnly=True,
                                         options={"effort": "high"})
        self.assertEqual(status, 202)
        self.assertIs(submitted["readOnly"], True)
        task = relay.wait_task(submitted["id"])
        self.assertEqual(json.loads(task["output"]),
                         ["--base", "--high", "--exclude-tools", "edit,write", "review"])

    def test_adds_the_profile_args_for_a_stdio_adapter_too(self):
        relay = self.start({"id": "fake", "type": "stdio", "command": PYTHON,
                            "args": ["-c", STDIO_ARGV, "--base"], "readOnly": {"args": NO_WRITE}})
        self.assertEqual(self.argv_of(relay, input="x", readOnly=True), ["--base", *NO_WRITE])

    def test_a_task_that_is_not_read_only_gets_no_profile_args(self):
        relay = self.start(fake(readOnly={"args": NO_WRITE}))
        for body in ({}, {"readOnly": False}):
            _, submitted = relay.submit(agentId="fake", input="edit", **body)
            self.assertNotIn("readOnly", submitted)
            task = relay.wait_task(submitted["id"])
            self.assertEqual(json.loads(task["output"]), ["--base", "edit"])

    def test_refuses_a_read_only_task_for_an_agent_without_a_profile_and_runs_nothing(self):
        relay = self.start([fake(), {"id": "hosted", "type": "http", "url": "http://127.0.0.1:9/run"}])
        for agent_id in ("fake", "hosted"):
            status, payload = relay.submit(agentId=agent_id, input="x", readOnly=True)
            self.assertEqual((status, payload["error"]), (400, "read_only_unsupported"), agent_id)
            self.assertIn(agent_id, payload["message"])
            self.assertIn("no readOnly profile", payload["message"])
        self.assertEqual(relay.request("GET", "/v1/tasks")[1]["tasks"], [])

    def test_fails_closed_when_read_only_is_not_a_boolean(self):
        relay = self.start(fake(readOnly={"args": NO_WRITE}))
        for bad in ("true", 1, None, [], {}):
            status, payload = relay.submit(agentId="fake", input="x", readOnly=bad)
            self.assertEqual((status, payload["error"]), (400, "invalid_read_only"), repr(bad))
        self.assertEqual(relay.request("GET", "/v1/tasks")[1]["tasks"], [])

    def test_a_retry_that_changes_read_only_is_a_conflict(self):
        relay = self.start(fake(readOnly={"args": NO_WRITE}))
        first = relay.submit(agentId="fake", input="x", requestId="r1", readOnly=True)
        self.assertEqual(first[0], 202)
        same = relay.submit(agentId="fake", input="x", requestId="r1", readOnly=True)
        self.assertEqual((same[0], same[1]["id"]), (200, first[1]["id"]))
        for body in ({}, {"readOnly": False}):
            status, payload = relay.submit(agentId="fake", input="x", requestId="r1", **body)
            self.assertEqual((status, payload["error"]), (409, "idempotency_conflict"), body)
        other = relay.submit(agentId="fake", input="y", requestId="r2")
        status, payload = relay.submit(agentId="fake", input="y", requestId="r2", readOnly=True)
        self.assertEqual((other[0], status, payload["error"]), (202, 409, "idempotency_conflict"))

    def test_lists_whether_each_agent_supports_read_only_and_whether_it_is_sandboxed(self):
        relay = self.start([
            fake(id="args-only", readOnly={"args": NO_WRITE}),
            fake(id="none"),
            {"id": "hosted", "type": "http", "url": "http://127.0.0.1:9/run"},
        ])
        listing = {entry["id"]: entry for entry in relay.request("GET", "/v1/agents")[1]["agents"]}
        self.assertEqual((listing["args-only"]["readOnly"], listing["args-only"]["readOnlySandboxed"]),
                         ("supported", False))
        for agent_id in ("none", "hosted"):
            self.assertEqual(listing[agent_id]["readOnly"], "unsupported")
            self.assertNotIn("readOnlySandboxed", listing[agent_id])

    @unittest.skipUnless(SANDBOX, "sandbox-exec is available on macOS only")
    def test_lists_a_sandboxed_profile_as_supported_on_macos(self):
        relay = self.start(fake(readOnly={"args": NO_WRITE, "sandbox": True}))
        entry = relay.request("GET", "/v1/agents")[1]["agents"][0]
        self.assertEqual((entry["readOnly"], entry["readOnlySandboxed"]), ("supported", True))

    def test_a_sandboxed_profile_is_unsupported_where_sandbox_exec_is_missing(self):
        agent = {"id": "pi", "readOnly": {"args": NO_WRITE, "sandbox": True}}
        with mock.patch.object(server, "_sandbox_available", return_value=False):
            message = server._read_only_refusal(agent)
            self.assertIn("sandbox-exec", message)
            self.assertIn("macOS", message)
            self.assertEqual(server._read_only_refusal({"id": "pi", "readOnly": {"args": NO_WRITE}}), None)
        with mock.patch.object(server, "_sandbox_available", return_value=True):
            self.assertIsNone(server._read_only_refusal(agent))

    @unittest.skipIf(SANDBOX, "on macOS the sandbox is available; the refusal is checked with a mock above")
    def test_refuses_a_sandboxed_read_only_task_where_sandbox_exec_is_missing(self):
        relay = self.start(fake(readOnly={"args": NO_WRITE, "sandbox": True}))
        status, payload = relay.submit(agentId="fake", input="x", readOnly=True)
        self.assertEqual((status, payload["error"]), (400, "read_only_unsupported"))
        self.assertIn("macOS", payload["message"])
        self.assertEqual(relay.request("GET", "/v1/agents")[1]["agents"][0]["readOnly"], "unsupported")

    def test_an_agent_never_receives_the_relay_token_in_its_environment(self):
        script = "import json, os; print(json.dumps(dict(os.environ)))"
        relay = Relay({"id": "env", "command": PYTHON, "args": ["-c", script]},
                      env={"A2A_RELAY_TOKEN": "second-relay-token"})
        self.addCleanup(relay.close)
        output = relay.run_task(agentId="env", input="x")["output"]
        names = json.loads(output)
        self.assertNotIn("AGENT_RELAY_TOKEN", names)
        self.assertNotIn("A2A_RELAY_TOKEN", names)
        self.assertNotIn(TOKEN, output)
        self.assertNotIn("second-relay-token", output)

    def test_the_sandbox_profile_names_the_token_and_port_only_as_parameters(self):
        awkward = '/tmp/a2a "q" (x) tok'
        with mock.patch.dict(os.environ, {"AGENT_RELAY_TOKEN_FILE": awkward}), \
                mock.patch.object(server, "PORT", 43999):
            argv = server._sandboxed({"id": "a"}, {"cwd": "/tmp"}, ["agent", "arg"])
        profile = argv[argv.index("-p") + 1]
        defines = [argv[index + 1] for index, flag in enumerate(argv) if flag == "-D"]
        self.assertEqual(argv[-2:], ["agent", "arg"])
        self.assertIn("RELAY_PORT=43999", defines)
        self.assertIn("TOKEN=" + os.path.realpath(awkward), defines)
        # Every directory above the token is a parameter too, so none can be moved aside.
        for parent in list(server.Path(os.path.realpath(awkward)).parents)[:-1]:
            self.assertIn(str(parent), [item.split("=", 1)[1] for item in defines], parent)
        for value in ("43999", "a2a", "tok", "/tmp", "private"):
            self.assertNotIn(value, profile)

    def test_the_sandbox_profile_also_protects_every_directory_above_the_protected_trees(self):
        outer = os.path.realpath(tempfile.mkdtemp(prefix="a2a-above-"))
        self.addCleanup(shutil.rmtree, outer, True)
        cwd, root = os.path.join(outer, "one", "cwd"), os.path.join(outer, "two", "root")
        os.makedirs(cwd)
        os.makedirs(root)
        argv = server._sandboxed({"id": "a", "allowedRoots": [root]}, {"cwd": cwd}, ["agent"])
        defines = [argv[index + 1].split("=", 1)[1] for index, flag in enumerate(argv) if flag == "-D"]
        for tree in (cwd, root):
            for parent in list(server.Path(tree).parents)[:-1]:
                self.assertIn(str(parent), defines, parent)

    def test_the_mcp_delegate_tool_passes_read_only_to_the_relay(self):
        relay = self.start(fake(readOnly={"args": NO_WRITE}))
        client = McpClient(relay.base)
        self.addCleanup(client.close)
        done = client.call_tool("delegate", {"agentId": "fake", "input": "review", "readOnly": True,
                                             "waitMs": 5000})["structuredContent"]
        self.assertIs(done["readOnly"], True)
        self.assertEqual(json.loads(done["output"]), ["--base", *NO_WRITE, "review"])
        plain = client.call_tool("delegate", {"agentId": "fake", "input": "x"})["structuredContent"]
        self.assertNotIn("readOnly", plain)


@unittest.skipUnless(SANDBOX, "the write-blocking sandbox needs sandbox-exec (macOS)")
class ReadOnlySandboxTests(unittest.TestCase):
    def setUp(self):
        # A name with a space, quotes and "=" shows the path is passed as data, never as profile text.
        self.cwd = tempfile.mkdtemp(prefix='a2a ro "q" =x ')
        self.state = tempfile.mkdtemp(prefix="a2a-state-")
        for path in (self.cwd, self.state):
            self.addCleanup(shutil.rmtree, path, True)
        with open(os.path.join(self.cwd, "victim.txt"), "w", encoding="utf-8") as handle:
            handle.write("original")

    def start(self, **extra):
        agent = {"id": "writer", "command": PYTHON, "args": ["-c", WRITER], "cwd": self.cwd,
                 "env": {"STATE_DIR": self.state}, "readOnly": {"sandbox": True}, **extra}
        relay = Relay(agent)
        self.addCleanup(relay.close)
        return relay

    def cwd_file(self, name):
        path = os.path.join(self.cwd, name)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_a_task_that_is_not_read_only_can_write_so_the_denial_below_comes_from_the_sandbox(self):
        task = self.start().run_task(agentId="writer", input="x")
        self.assertEqual(task["status"], "completed", task)
        self.assertEqual((self.cwd_file("victim.txt"), self.cwd_file("new.txt")), ("changed", "new"))

    def test_a_read_only_task_cannot_change_its_cwd_but_can_write_its_own_state(self):
        task = self.start().run_task(agentId="writer", input="x", readOnly=True)
        self.assertEqual(task["status"], "failed", task)
        self.assertIn("Operation not permitted", task["error"])
        self.assertEqual((self.cwd_file("victim.txt"), self.cwd_file("new.txt")), ("original", None))
        with open(os.path.join(self.state, "state.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "state")

    def test_a_sandboxed_read_only_task_needs_a_cwd_to_protect(self):
        relay = Relay({"id": "writer", "command": PYTHON, "args": ["-c", "print(1)"],
                       "allowedRoots": [self.cwd], "readOnly": {"sandbox": True}})
        self.addCleanup(relay.close)
        status, payload = relay.submit(agentId="writer", input="x", readOnly=True)
        self.assertEqual((status, payload["error"]), (400, "cwd_required"))
        self.assertEqual(relay.request("GET", "/v1/tasks")[1]["tasks"], [])
        self.assertEqual(relay.run_task(agentId="writer", input="x", readOnly=True, cwd=self.cwd)["status"],
                         "completed")

    def test_a_read_only_task_cannot_write_in_the_agents_allowed_roots_either(self):
        root = os.path.realpath(tempfile.mkdtemp(prefix="a2a-root-"))
        self.addCleanup(shutil.rmtree, root, True)
        sub = os.path.join(root, "sub")
        os.mkdir(sub)
        script = "open(%r, 'w').write('x')" % os.path.join(root, "sibling.txt")
        relay = Relay({"id": "writer", "command": PYTHON, "args": ["-c", script],
                       "allowedRoots": [root], "readOnly": {"sandbox": True}})
        self.addCleanup(relay.close)
        task = relay.run_task(agentId="writer", input="x", cwd=sub, readOnly=True)
        self.assertEqual(task["status"], "failed", task)
        self.assertFalse(os.path.exists(os.path.join(root, "sibling.txt")))
        allowed = relay.run_task(agentId="writer", input="x", cwd=sub)
        self.assertEqual(allowed["status"], "completed", allowed)
        self.assertTrue(os.path.exists(os.path.join(root, "sibling.txt")))

    def test_a_read_only_task_cannot_hard_link_a_file_in_its_cwd_to_a_name_outside(self):
        link = os.path.join(self.state, "link.txt")
        script = "import os; os.link('victim.txt', %r)" % link
        relay = Relay({"id": "linker", "command": PYTHON, "args": ["-c", script], "cwd": self.cwd,
                       "readOnly": {"sandbox": True}})
        self.addCleanup(relay.close)
        task = relay.run_task(agentId="linker", input="x", readOnly=True)
        self.assertEqual(task["status"], "failed", task)
        self.assertFalse(os.path.exists(link))
        control = relay.run_task(agentId="linker", input="x")
        self.assertEqual(control["status"], "completed", control)
        self.assertTrue(os.path.exists(link))

    def test_a_read_only_stdio_adapter_cannot_change_its_cwd_either(self):
        script = (
            "import json, sys\n"
            "sys.stdin.read()\n"
            "open('new.txt', 'w').write('new')\n"
            "print(json.dumps({'protocolVersion': 'relay.adapter/v1', 'status': 'completed', 'output': ''}))\n"
        )
        relay = Relay({"id": "wrapped", "type": "stdio", "command": PYTHON, "args": ["-c", script],
                       "cwd": self.cwd, "readOnly": {"sandbox": True}})
        self.addCleanup(relay.close)
        task = relay.run_task(agentId="wrapped", input="x", readOnly=True)
        self.assertEqual(task["status"], "failed", task)
        self.assertIsNone(self.cwd_file("new.txt"))

    def test_a_read_only_task_cannot_rename_a_directory_above_its_cwd_to_write_through_the_new_path(self):
        outer = os.path.realpath(tempfile.mkdtemp(prefix="a2a-above-"))
        self.addCleanup(shutil.rmtree, outer, True)
        above, moved = os.path.join(outer, "review"), os.path.join(outer, "moved")
        tree = os.path.join(above, "tree")
        os.makedirs(tree)
        write(os.path.join(tree, "victim.txt"), "original")
        script = (f"import os\nos.rename({above!r}, {moved!r})\n"
                  f"open({os.path.join(moved, 'tree', 'victim.txt')!r}, 'w').write('changed')\n")
        relay = Relay({"id": "mover", "command": PYTHON, "args": ["-c", script], "cwd": tree,
                       "readOnly": {"sandbox": True}})
        self.addCleanup(relay.close)
        blocked = relay.run_task(agentId="mover", input="x", readOnly=True)
        self.assertEqual(blocked["status"], "failed", blocked)
        self.assertIn("Operation not permitted", blocked["error"])
        self.assertTrue(os.path.isdir(tree), "the read-only task moved a directory above its cwd")
        with open(os.path.join(tree, "victim.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "original")
        # Without readOnly the same script works, so the block above comes from the sandbox.
        control = relay.run_task(agentId="mover", input="x")
        self.assertEqual(control["status"], "completed", control)
        with open(os.path.join(moved, "tree", "victim.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "changed")

    def test_a_read_only_task_can_still_write_beside_its_cwd_inside_a_directory_above_it(self):
        outer = os.path.realpath(tempfile.mkdtemp(prefix="a2a-above-"))
        self.addCleanup(shutil.rmtree, outer, True)
        tree = os.path.join(outer, "review", "tree")
        os.makedirs(tree)
        beside = os.path.join(outer, "review", "beside.txt")
        relay = Relay({"id": "beside", "command": PYTHON, "cwd": tree, "readOnly": {"sandbox": True},
                       "args": ["-c", f"open({beside!r}, 'w').write('ok')"]})
        self.addCleanup(relay.close)
        task = relay.run_task(agentId="beside", input="x", readOnly=True)
        self.assertEqual(task["status"], "completed", task)
        self.assertTrue(os.path.exists(beside))

    def test_a_read_only_task_cannot_rename_a_directory_above_an_allowed_root_either(self):
        outer = os.path.realpath(tempfile.mkdtemp(prefix="a2a-above-"))
        self.addCleanup(shutil.rmtree, outer, True)
        above, moved = os.path.join(outer, "shared"), os.path.join(outer, "moved")
        root = os.path.join(above, "root")
        os.makedirs(os.path.join(root, "sub"))
        write(os.path.join(root, "victim.txt"), "original")
        script = (f"import os\nos.rename({above!r}, {moved!r})\n"
                  f"open({os.path.join(moved, 'root', 'victim.txt')!r}, 'w').write('changed')\n")
        relay = Relay({"id": "mover", "command": PYTHON, "args": ["-c", script], "allowedRoots": [root],
                       "readOnly": {"sandbox": True}})
        self.addCleanup(relay.close)
        sub = os.path.join(root, "sub")
        blocked = relay.run_task(agentId="mover", input="x", cwd=sub, readOnly=True)
        self.assertEqual(blocked["status"], "failed", blocked)
        self.assertIn("Operation not permitted", blocked["error"])
        self.assertTrue(os.path.isdir(above), "the read-only task moved a directory above an allowed root")
        control = relay.run_task(agentId="mover", input="x", cwd=sub)
        self.assertEqual(control["status"], "completed", control)
        with open(os.path.join(moved, "root", "victim.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "changed")


@unittest.skipUnless(SANDBOX, "the relay-access sandbox rules need sandbox-exec (macOS)")
class ReadOnlyRelayAccessTests(unittest.TestCase):
    """A sandboxed agent runs as the relay's user, so it must not reach the relay's token or port:
    either would let it ask the relay to write for it, outside the sandbox."""

    def setUp(self):
        self.cwd = os.path.realpath(tempfile.mkdtemp(prefix="a2a-ro-cwd-"))
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="a2a-token-"))
        for path in (self.cwd, self.base):
            self.addCleanup(shutil.rmtree, path, True)
        with open(os.path.join(self.cwd, "victim.txt"), "w", encoding="utf-8") as handle:
            handle.write("original")
        self.token_dir = os.path.join(self.base, "token dir")
        os.mkdir(self.token_dir, 0o700)
        self.token_file = os.path.join(self.token_dir, "token")
        fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(TOKEN + "\n")

    def start(self, script, *more_agents):
        """A relay that loaded its token from a file, with one sandbox-capable agent running ``script``."""
        agent = {"id": "probe", "command": PYTHON, "args": ["-c", script], "cwd": self.cwd,
                 "env": {"TOKEN_FILE": self.token_file, "TOKEN_DIR": self.token_dir, "BASE": self.base},
                 "inheritEnv": ["A2A_RELAY_PORT"], "readOnly": {"sandbox": True}}
        relay = Relay([agent, *more_agents], env={"AGENT_RELAY_TOKEN": "", "A2A_RELAY_TOKEN": "",
                                                  "AGENT_RELAY_TOKEN_FILE": self.token_file})
        self.addCleanup(relay.close)
        return relay

    def cwd_file(self, name):
        with open(os.path.join(self.cwd, name), encoding="utf-8") as handle:
            return handle.read()

    def assert_only_an_unrestricted_run_gets_the_token(self, script):
        relay = self.start(script)
        blocked = relay.run_task(agentId="probe", input="x", readOnly=True)
        self.assertEqual(blocked["status"], "failed", blocked)
        self.assertIn("Operation not permitted", blocked["error"])
        self.assertNotIn(TOKEN, json.dumps(blocked))
        self.assertTrue(os.path.exists(self.token_file), "the sandboxed agent moved the token file")
        # The same script works without readOnly, so the failure above comes from the sandbox.
        control = relay.run_task(agentId="probe", input="x")
        self.assertEqual((control["status"], control["output"].strip()), ("completed", TOKEN), control)

    def assert_only_an_unrestricted_run_connects(self, target_for):
        relay = self.start(CONNECT)
        target = target_for(relay)
        blocked = relay.run_task(agentId="probe", input=target, readOnly=True)
        self.assertEqual(blocked["status"], "failed", blocked)
        self.assertIn("Operation not permitted", blocked["error"])
        control = relay.run_task(agentId="probe", input=target)
        self.assertEqual((control["status"], control["output"].strip()), ("completed", "connected"), control)

    def test_a_read_only_task_cannot_read_the_relay_token_file(self):
        self.assert_only_an_unrestricted_run_gets_the_token(READ_TOKEN)

    def test_a_read_only_task_cannot_reach_the_token_through_a_hard_link(self):
        self.assert_only_an_unrestricted_run_gets_the_token(LINK_TOKEN)

    def test_a_read_only_task_cannot_move_the_token_file_aside_and_read_it(self):
        self.assert_only_an_unrestricted_run_gets_the_token(MOVE_TOKEN_FILE)

    def test_a_read_only_task_cannot_move_a_directory_above_the_token_aside(self):
        self.assert_only_an_unrestricted_run_gets_the_token(MOVE_TOKEN_DIR)

    def token_text(self):
        with open(self.token_file, encoding="utf-8") as handle:
            return handle.read().strip()

    def test_a_read_only_task_cannot_overwrite_or_delete_the_token_file(self):
        overwrite = "import os; open(os.environ['TOKEN_FILE'], 'w').write('replaced')"
        delete = "import os; os.unlink(os.environ['TOKEN_FILE'])"
        relay = self.start(overwrite, {"id": "deleter", "command": PYTHON, "cwd": self.cwd,
                                       "args": ["-c", delete], "env": {"TOKEN_FILE": self.token_file},
                                       "readOnly": {"sandbox": True}})
        for agent_id in ("probe", "deleter"):
            task = relay.run_task(agentId=agent_id, input="x", readOnly=True)
            self.assertEqual(task["status"], "failed", task)
            self.assertIn("Operation not permitted", task["error"])
            self.assertEqual(self.token_text(), TOKEN)
        # Without readOnly both succeed, so the failures above come from the sandbox.
        self.assertEqual(relay.run_task(agentId="probe", input="x")["status"], "completed")
        self.assertEqual(self.token_text(), "replaced")
        self.assertEqual(relay.run_task(agentId="deleter", input="x")["status"], "completed")
        self.assertFalse(os.path.exists(self.token_file))

    def test_a_read_only_task_cannot_connect_to_the_relay_port_on_ipv4(self):
        self.assert_only_an_unrestricted_run_connects(lambda relay: f"127.0.0.1:{relay.port}")

    def test_a_read_only_task_cannot_connect_to_the_relay_port_on_ipv6(self):
        listener = socket.socket(socket.AF_INET6)
        self.addCleanup(listener.close)

        def target(relay):
            try:
                listener.bind(("::1", relay.port))
            except OSError as exc:
                self.skipTest(f"cannot listen on ::1 port {relay.port}: {exc}")
            listener.listen(5)
            return f"::1:{relay.port}"

        self.assert_only_an_unrestricted_run_connects(target)

    def test_a_read_only_task_can_still_reach_other_local_ports(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        relay = self.start(CONNECT)
        task = relay.run_task(agentId="probe", input=f"127.0.0.1:{listener.getsockname()[1]}", readOnly=True)
        self.assertEqual((task["status"], task["output"].strip()), ("completed", "connected"), task)

    def test_a_read_only_agent_cannot_get_a_writable_agent_to_change_its_cwd_through_the_relay(self):
        mutator = {"id": "mutator", "command": PYTHON, "cwd": self.cwd,
                   "args": ["-c", "open('victim.txt', 'w').write('changed')"]}
        relay = self.start(ATTACK, mutator)
        blocked = relay.run_task(agentId="probe", input="x", readOnly=True)
        self.assertEqual(blocked["status"], "failed", blocked)
        self.assertEqual(self.cwd_file("victim.txt"), "original")
        # Without readOnly the same agent can read the token and use the relay, so the block is real.
        control = relay.run_task(agentId="probe", input="x")
        self.assertEqual((control["status"], control["output"].strip()), ("completed", "completed"), control)
        self.assertEqual(self.cwd_file("victim.txt"), "changed")

    def test_a_read_only_task_does_not_get_the_token_even_when_the_owner_passes_it_on(self):
        script = ("import json, os; print(json.dumps([name for name in "
                  "('AGENT_RELAY_TOKEN', 'A2A_RELAY_TOKEN') if name in os.environ]))")
        agent = {"id": "probe", "command": PYTHON, "args": ["-c", script], "cwd": self.cwd,
                 "inheritEnv": ["AGENT_RELAY_TOKEN"], "env": {"A2A_RELAY_TOKEN": "literal"},
                 "readOnly": {"sandbox": True}}
        relay = Relay(agent)  # the helper starts the relay with AGENT_RELAY_TOKEN in its environment
        self.addCleanup(relay.close)
        control = relay.run_task(agentId="probe", input="x")
        self.assertEqual(json.loads(control["output"]), ["AGENT_RELAY_TOKEN", "A2A_RELAY_TOKEN"], control)
        task = relay.run_task(agentId="probe", input="x", readOnly=True)
        self.assertEqual((task["status"], json.loads(task["output"])), ("completed", []), task)


def run_git(cwd, *args):
    """Run git in ``cwd`` with a fixed identity and none of the caller's GIT_* variables."""
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    done = subprocess.run(["git", "-c", "user.name=tester", "-c", "user.email=tester@example.com", *args],
                          cwd=cwd, env=env, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def write(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


@unittest.skipUnless(GIT, "git is not installed")
class GitFixture(unittest.TestCase):
    """A repository with two commits and a second worktree whose index lives outside the worktree."""

    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="a2a-git-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.main = os.path.join(self.base, "main repo")
        self.worktree = os.path.join(self.base, "review tree")
        self.outside = os.path.join(self.base, "outside")
        os.mkdir(self.main)
        os.mkdir(self.outside)
        run_git(self.main, "init", "-q")
        for text in ("one", "two"):
            write(os.path.join(self.main, "f.txt"), text)
            run_git(self.main, "add", "f.txt")
            run_git(self.main, "commit", "-q", "-m", text)
        run_git(self.main, "worktree", "add", "-q", "-b", "review", self.worktree)
        write(os.path.join(self.worktree, "f.txt"), "three")  # an unstaged change for diff to show
        os.mkdir(os.path.join(self.worktree, "sub"))

    def git_path(self, cwd, name):
        return os.path.realpath(run_git(cwd, "rev-parse", "--path-format=absolute", "--git-path", name))

    def index_bytes(self):
        with open(self.git_path(self.worktree, "index"), "rb") as handle:
            return handle.read()


class GitMetadataTests(GitFixture):
    def test_finds_the_git_directory_and_the_shared_directory_of_a_worktree(self):
        self.assertEqual(server._git_metadata(self.worktree),
                         [os.path.join(self.main, ".git", "worktrees", "review-tree"),
                          os.path.join(self.main, ".git")])
        self.assertEqual(server._git_metadata(os.path.join(self.worktree, "sub")),
                         server._git_metadata(self.worktree))

    def test_finds_the_git_directory_of_a_plain_checkout_from_a_subdirectory_too(self):
        os.mkdir(os.path.join(self.main, "sub"))
        git_dir = os.path.join(self.main, ".git")
        for cwd in (self.main, os.path.join(self.main, "sub")):
            self.assertEqual(set(server._git_metadata(cwd)), {git_dir})

    def test_resolves_symbolic_links_to_real_paths(self):
        link = os.path.join(self.base, "link")
        os.symlink(self.worktree, link)
        self.assertEqual(server._git_metadata(link), server._git_metadata(self.worktree))

    def test_adds_nothing_outside_a_repository_or_for_a_missing_directory(self):
        self.assertEqual(server._git_metadata(self.outside), [])
        self.assertEqual(server._git_metadata(os.path.join(self.base, "missing")), [])

    def test_asks_git_nothing_outside_a_repository_so_a_missing_git_does_not_matter(self):
        with mock.patch.object(server.subprocess, "run", side_effect=FileNotFoundError("git")) as run:
            self.assertEqual(server._git_metadata(self.outside), [])
        run.assert_not_called()

    def test_refuses_to_guess_inside_a_repository_when_git_is_missing_hangs_or_fails(self):
        for problem in (FileNotFoundError("git"), subprocess.TimeoutExpired("git", 1), PermissionError("git")):
            with mock.patch.object(server.subprocess, "run", side_effect=problem):
                with self.assertRaises(server.GitUnknown, msg=repr(problem)):
                    server._git_metadata(self.worktree)
        answers = [
            subprocess.CompletedProcess(["git"], 128, stdout=os.fsencode(self.main) + b"\n",
                                        stderr=b"fatal: detected dubious ownership"),
            subprocess.CompletedProcess(["git"], 0, stdout=b"\n", stderr=b""),
            subprocess.CompletedProcess(["git"], 0, stdout=b"", stderr=b""),
        ]
        for answer in answers:
            with mock.patch.object(server.subprocess, "run", return_value=answer):
                with self.assertRaises(server.GitUnknown, msg=answer):
                    server._git_metadata(self.worktree)
        with mock.patch.object(server.subprocess, "run", return_value=answers[0]):
            with self.assertRaisesRegex(server.GitUnknown, "dubious ownership"):
                server._git_metadata(self.worktree)

    def test_gives_git_no_time_once_the_deadline_has_passed(self):
        with mock.patch.object(server.subprocess, "run") as run:
            with self.assertRaises(server.GitUnknown):
                server._git_metadata(self.worktree, deadline=time.monotonic() - 1)
        run.assert_not_called()

    def test_limits_each_git_call_to_the_time_the_task_has_left(self):
        done = subprocess.CompletedProcess(["git"], 0, stdout=os.fsencode(self.main) + b"/.git\n", stderr=b"")
        with mock.patch.object(server.subprocess, "run", return_value=done) as run:
            server._git_metadata(self.worktree, deadline=time.monotonic() + 1.5)
            server._git_metadata(self.worktree)
        limits = [call.kwargs["timeout"] for call in run.call_args_list]
        self.assertTrue(all(0 < limit <= 1.5 for limit in limits[:2]), limits)
        self.assertEqual(limits[2:], [server.GIT_TIMEOUT_S] * 2)

    def test_keeps_a_git_path_that_contains_a_newline_whole(self):
        repo = os.path.join(self.base, "line one\nline two")
        os.mkdir(repo)
        run_git(repo, "init", "-q")
        self.assertEqual(server._git_metadata(repo), [os.path.join(repo, ".git")])

    def test_ignores_a_git_directory_set_in_the_relays_environment(self):
        expected = server._git_metadata(self.worktree)
        with mock.patch.dict(os.environ, {"GIT_DIR": os.path.join(self.main, ".git"),
                                          "GIT_WORK_TREE": self.main, "GIT_COMMON_DIR": self.outside}):
            self.assertEqual(server._git_metadata(self.worktree), expected)

    def test_ignores_git_configuration_set_in_the_relays_environment(self):
        expected = server._git_metadata(self.worktree)
        for name, value in (("GIT_CONFIG_COUNT", "invalid"), ("GIT_CONFIG_PARAMETERS", "'unterminated"),
                            ("GIT_CONFIG", os.path.join(self.base, "missing")),
                            ("GIT_CONFIG_GLOBAL", os.path.join(self.base, "missing"))):
            with mock.patch.dict(os.environ, {name: value}):
                self.assertEqual(server._git_metadata(self.worktree), expected, name)

    def test_the_sandbox_profile_gets_the_git_directories_as_parameters(self):
        argv = server._sandboxed({"id": "a"}, {"cwd": self.worktree}, ["agent"])
        profile = argv[argv.index("-p") + 1]
        defines = [argv[index + 1].split("=", 1)[1] for index, flag in enumerate(argv) if flag == "-D"]
        for path in server._git_metadata(self.worktree):
            self.assertIn(path, defines)
        for name in ("main repo", "review tree"):
            self.assertNotIn(name, profile)


@unittest.skipUnless(SANDBOX, "the write-blocking sandbox needs sandbox-exec (macOS)")
class ReadOnlyGitMetadataTests(GitFixture):
    """A worktree keeps its index and the shared refs and objects outside its own directory, so a
    deny limited to the cwd would let a read-only agent rewrite them (the 2026-10-17 incident:
    git --work-tree=/tmp/x checkout <old> -- <path> sent the files to /tmp and rewrote the index)."""

    def start(self, cwd=None, env=None):
        agent = {"id": "git", "command": PYTHON, "args": ["-c", GIT_RUN], "cwd": cwd or self.worktree,
                 "readOnly": {"sandbox": True}}
        relay = Relay(agent, env=env)
        self.addCleanup(relay.close)
        return relay

    def delegate(self, relay, *commands, read_only=True):
        body = {"agentId": "git", "input": json.dumps(commands)}
        if read_only:
            body["readOnly"] = True
        return relay.run_task(**body)

    def test_the_index_of_the_worktree_is_outside_the_worktree(self):
        index = self.git_path(self.worktree, "index")
        self.assertTrue(index.startswith(os.path.join(self.main, ".git", "worktrees") + os.sep), index)

    def assert_blocked_and_only_a_writable_run_changes_the_index(self, *commands, cwd=None, env=None):
        relay = self.start(cwd, env)
        before = self.index_bytes()
        blocked = self.delegate(relay, *commands)
        self.assertEqual(blocked["status"], "failed", blocked)
        self.assertIn("Operation not permitted", blocked["error"])
        self.assertEqual(self.index_bytes(), before, "the read-only task changed the worktree's index")
        # Without readOnly the same commands succeed and change the index, so the block comes from the sandbox.
        control = self.delegate(relay, *commands, read_only=False)
        self.assertEqual(control["status"], "completed", control)
        self.assertNotEqual(self.index_bytes(), before)

    def test_a_read_only_task_cannot_rewrite_the_index_with_another_work_tree(self):
        self.assert_blocked_and_only_a_writable_run_changes_the_index(
            ["--work-tree", self.outside, "checkout", "HEAD~1", "--", "f.txt"])

    def test_a_read_only_task_stays_protected_when_the_relays_git_configuration_is_broken(self):
        self.assert_blocked_and_only_a_writable_run_changes_the_index(
            ["add", "f.txt"], env={"GIT_CONFIG_COUNT": "invalid"})

    def fake_git(self, script):
        """A PATH whose first entry holds a ``git`` that runs the shell ``script``.

        The relay itself runs ``git log`` once at startup to report its revision (``/healthz``), so
        that command goes to the real git: a fake that hangs there would keep the relay from starting
        and the test would never reach the read-only git lookup it means to break.
        """
        directory = os.path.join(self.base, "fake git")
        os.mkdir(directory)
        path = os.path.join(directory, "git")
        passthrough = f'case " $* " in *" log "*) exec "{GIT}" "$@";; esac\n'
        write(path, "#!/bin/sh\n" + passthrough + script + "\n")
        os.chmod(path, 0o755)
        return directory + os.pathsep + os.environ["PATH"]

    def marker_relay(self, path_env):
        """A relay whose agents write the path in their input; ``in-repo`` runs in the worktree and
        ``outside`` in a directory that is not in any repository. Both write outside what they protect."""
        agents = [{"id": name, "command": PYTHON, "cwd": cwd, "readOnly": {"sandbox": True},
                   "args": ["-c", "import sys; open(sys.argv[1], 'w').write('ran')"]}
                  for name, cwd in (("in-repo", self.worktree), ("outside", self.outside))]
        relay = Relay(agents, env={"PATH": path_env})
        self.addCleanup(relay.close)
        return relay

    def assert_refused_and_only_an_unrestricted_run_starts(self, relay, expected_in_error):
        marker = os.path.join(self.base, "ran")
        refused = relay.run_task(agentId="in-repo", input=marker, readOnly=True)
        self.assertEqual(refused["status"], "failed", refused)
        self.assertTrue(refused["error"].startswith("read_only_git_unknown"), refused)
        self.assertIn(expected_in_error, refused["error"])
        self.assertFalse(os.path.exists(marker), "the read-only task ran with less protection")
        # Without readOnly the task needs no git, so it runs: the refusal above is about protection.
        control = relay.run_task(agentId="in-repo", input=marker)
        self.assertEqual(control["status"], "completed", control)
        self.assertTrue(os.path.exists(marker))

    def test_a_read_only_task_in_a_repository_is_refused_when_git_is_missing(self):
        relay = self.marker_relay(os.path.join(self.base, "no git here"))
        self.assert_refused_and_only_an_unrestricted_run_starts(relay, "git")

    def test_a_read_only_task_in_a_repository_is_refused_when_git_fails(self):
        relay = self.marker_relay(self.fake_git("echo 'fatal: detected dubious ownership' >&2; exit 128"))
        self.assert_refused_and_only_an_unrestricted_run_starts(relay, "dubious ownership")

    def test_a_read_only_task_outside_a_repository_runs_even_when_git_is_missing(self):
        relay = self.marker_relay(os.path.join(self.base, "no git here"))
        marker = os.path.join(self.base, "ran")
        task = relay.run_task(agentId="outside", input=marker, readOnly=True)
        self.assertEqual(task["status"], "completed", task)
        self.assertTrue(os.path.exists(marker))

    def test_a_read_only_task_ends_timed_out_without_starting_when_git_uses_up_its_time(self):
        relay = self.marker_relay(self.fake_git("exec sleep 30"))
        marker = os.path.join(self.base, "ran")
        started = time.monotonic()
        task = relay.run_task(agentId="in-repo", input=marker, readOnly=True, timeoutMs=1000)
        self.assertEqual(task["status"], "timed_out", task)
        self.assertLess(time.monotonic() - started, server.GIT_TIMEOUT_S - 1, "git was not limited to the task's time")
        self.assertFalse(os.path.exists(marker), "the adapter started after its deadline")

    def test_a_read_only_task_cannot_stage_a_change_in_the_worktree(self):
        self.assert_blocked_and_only_a_writable_run_changes_the_index(["add", "f.txt"])
        self.assertIn("f.txt", run_git(self.worktree, "diff", "--cached", "--name-only"))

    def test_a_read_only_task_cannot_update_the_index_directly(self):
        blob = run_git(self.worktree, "rev-parse", "HEAD~1:f.txt")
        self.assert_blocked_and_only_a_writable_run_changes_the_index(
            ["update-index", "--cacheinfo", f"100644,{blob},f.txt"])

    def test_a_read_only_task_cannot_do_it_from_a_subdirectory_of_the_worktree_either(self):
        self.assert_blocked_and_only_a_writable_run_changes_the_index(
            ["--work-tree", self.outside, "checkout", "HEAD~1", "--", "f.txt"],
            cwd=os.path.join(self.worktree, "sub"))

    def test_a_read_only_task_cannot_move_a_branch_or_add_objects_in_the_shared_repository(self):
        relay = self.start()
        config = os.path.join(self.main, ".git", "config")
        with open(config, "rb") as handle:
            config_before = handle.read()
        branch_before = run_git(self.main, "rev-parse", "review")
        objects_before = run_git(self.main, "count-objects", "-v")
        for command in (["update-ref", "refs/heads/review", "HEAD~1"], ["branch", "other", "HEAD~1"],
                        ["commit", "--allow-empty", "-q", "-m", "x"], ["hash-object", "-w", "f.txt"],
                        ["config", "user.name", "changed"]):
            task = self.delegate(relay, command)
            self.assertEqual(task["status"], "failed", (command, task))
            self.assertIn("Operation not permitted", task["error"], command)
        self.assertEqual(run_git(self.main, "rev-parse", "review"), branch_before)
        self.assertNotIn("other", run_git(self.main, "branch", "--list"))
        self.assertEqual(run_git(self.main, "count-objects", "-v"), objects_before)
        with open(config, "rb") as handle:
            self.assertEqual(handle.read(), config_before)

    def test_a_read_only_task_cannot_rename_the_main_repository_that_holds_the_worktrees_git_metadata(self):
        moved = os.path.join(self.base, "moved main")
        head = os.path.join(self.main, ".git", "HEAD")
        with open(head, "rb") as handle:
            before = handle.read()
        script = (f"import os\nos.rename({self.main!r}, {moved!r})\n"
                  f"open({os.path.join(moved, '.git', 'HEAD')!r}, 'w').write('ref: refs/heads/other')\n")
        relay = Relay({"id": "mover", "command": PYTHON, "args": ["-c", script], "cwd": self.worktree,
                       "readOnly": {"sandbox": True}})
        self.addCleanup(relay.close)
        blocked = relay.run_task(agentId="mover", input="x", readOnly=True)
        self.assertEqual(blocked["status"], "failed", blocked)
        self.assertIn("Operation not permitted", blocked["error"])
        with open(head, "rb") as handle:
            self.assertEqual(handle.read(), before)
        # Without readOnly the script works, so the block above comes from the sandbox.
        control = relay.run_task(agentId="mover", input="x")
        self.assertEqual(control["status"], "completed", control)
        self.assertTrue(os.path.isdir(moved))

    def test_a_read_only_task_can_still_read_the_history_and_the_changes(self):
        relay = self.start()
        task = self.delegate(relay, ["log", "--oneline"], ["show", "HEAD~1", "--stat"], ["diff"],
                        ["diff", "HEAD~1", "HEAD"], ["status", "--short"], ["rev-parse", "HEAD"])
        self.assertEqual(task["status"], "completed", task)
        self.assertIn(" two", task["output"])  # git log
        self.assertIn("-two", task["output"])  # git diff shows the unstaged change: two -> three
        self.assertIn("+three", task["output"])
        self.assertIn("f.txt", task["output"])  # git show --stat, git status

    def test_a_read_only_task_still_cannot_change_a_file_in_its_worktree(self):
        relay = self.start()
        task = self.delegate(relay, ["checkout", "HEAD~1", "--", "f.txt"])
        self.assertEqual(task["status"], "failed", task)
        with open(os.path.join(self.worktree, "f.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "three")


class ReadOnlyRegistryTests(unittest.TestCase):
    def load(self, agent):
        directory = tempfile.mkdtemp(prefix="a2a-readonly-")
        self.addCleanup(shutil.rmtree, directory, True)
        config = os.path.join(directory, "agents.json")
        with open(config, "w", encoding="utf-8") as handle:
            json.dump({"agents": [agent]}, handle)
        return server.load_registry(config)

    def test_accepts_a_profile_with_args_a_sandbox_or_both(self):
        for profile in ({"args": NO_WRITE}, {"sandbox": True}, {"args": NO_WRITE, "sandbox": True},
                        {"args": NO_WRITE, "sandbox": False}):
            self.assertIn("fake", self.load(fake(readOnly=profile)), profile)

    def test_rejects_a_malformed_profile_with_a_clear_error(self):
        cases = [
            ("readOnly must be an object", ["args"]),
            ("readOnly has an unknown field sandbx", {"sandbx": True}),
            ("readOnly args must be an array of strings", {"args": "--no-write"}),
            ("readOnly args must be an array of strings", {"args": ["a", 1]}),
            ("readOnly sandbox must be true or false", {"args": NO_WRITE, "sandbox": "yes"}),
            ("readOnly must set args, sandbox, or both", {}),
            ("readOnly must set args, sandbox, or both", {"args": [], "sandbox": False}),
        ]
        for message, profile in cases:
            with self.subTest(profile=profile):
                with self.assertRaisesRegex(ValueError, f"Invalid readOnly: agent fake: {message}"):
                    self.load(fake(readOnly=profile))

    def test_rejects_a_profile_on_an_http_agent(self):
        with self.assertRaisesRegex(ValueError, "agent remote: readOnly needs a command or stdio agent"):
            self.load({"id": "remote", "type": "http", "url": "http://127.0.0.1:9000/run",
                       "readOnly": {"args": NO_WRITE}})


if __name__ == "__main__":
    unittest.main()
