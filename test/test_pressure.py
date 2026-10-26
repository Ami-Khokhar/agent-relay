"""Device pressure: the reading, the preflight refusal, the kill button, and the guard."""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from relay_helpers import McpClient, PYTHON, Relay
import pressure

ECHO = "import sys; sys.stdout.write(sys.argv[1])"
SLOW = "import time; time.sleep(30)"
# Slow the first time only, so a killed run can be resumed to completion quickly.
SLOW_ONCE = (
    "import os, pathlib, time\n"
    "mark = pathlib.Path(os.environ['WF_MARK'])\n"
    "if not mark.exists():\n"
    "    mark.write_text('1')\n"
    "    time.sleep(30)\n"
    "print('finished')\n"
)
# Stand in for the two probes the relay reads. Each answers from a file in this directory, so a
# test changes what the host "reports" by writing one line.
SYSCTL_STUB = """#!/bin/sh
echo "sysctl $1 $2" >> "@DIR@/calls"
case "$2" in
  vm.memory_pressure) level=$(cat "@DIR@/level"); echo "level=$level" >> "@DIR@/calls"; echo "$level" ;;
  kern.memorystatus_level) cat "@DIR@/free" ;;
  vm.swapusage) cat "@DIR@/swap" ;;
  *) exit 1 ;;
esac
"""
PMSET_STUB = """#!/bin/sh
echo "pmset $1 $2" >> "@DIR@/calls"
cat "@DIR@/therm"
"""
SWAP_IDLE = "total = 2048.00M  used = 100.00M  free = 1948.00M  (encrypted)"
NO_THERM = "Note: No thermal warning level has been recorded"


def echo(agent_id="echo"):
    return {"id": agent_id, "command": PYTHON, "args": ["-c", ECHO]}


def steps_of(workflow):
    return {step["id"]: step for step in workflow["steps"]}


class PressureTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="a2a-pressure-")
        self.state = tempfile.mkdtemp(prefix="a2a-pressure-state-")
        self.stubs = os.path.join(self.dir, "stubs")
        os.makedirs(self.stubs)
        self.set_signal("level", "0")
        self.set_signal("free", "60")
        self.set_signal("swap", SWAP_IDLE)
        self.set_signal("therm", NO_THERM)
        for name, body in (("sysctl", SYSCTL_STUB), ("pmset", PMSET_STUB)):
            path = os.path.join(self.stubs, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(body.replace("@DIR@", self.stubs))
            os.chmod(path, 0o755)
        self.relays = []

    def tearDown(self):
        for relay in self.relays:
            relay.close()
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.state, ignore_errors=True)

    def set_signal(self, name, text):
        with open(os.path.join(self.stubs, name), "w", encoding="utf-8") as handle:
            handle.write(text + "\n")

    def probe_calls(self):
        path = os.path.join(self.stubs, "calls")
        if not os.path.exists(path):
            return 0
        with open(path, encoding="utf-8") as handle:
            return len(handle.readlines())

    def level_samples(self, text):
        """How many times a probe read the memory-pressure level and got `text`."""
        path = os.path.join(self.stubs, "calls")
        if not os.path.exists(path):
            return 0
        with open(path, encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip() == text)

    def wait_level_sample(self, text, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.level_samples(text):
                return
            time.sleep(0.02)
        raise RuntimeError(f"no probe ever read the level {text}")

    def start(self, agents, env=None):
        relay = Relay(agents, env={
            # The probes are named, not found on PATH: the relay never searches PATH for them.
            "AGENT_RELAY_SYSCTL": os.path.join(self.stubs, "sysctl"),
            "AGENT_RELAY_PMSET": os.path.join(self.stubs, "pmset"),
            "AGENT_RELAY_PRESSURE_GUARD": "1",
            "AGENT_RELAY_STATE_DIR": self.state,
            "AGENT_RELAY_PRESSURE_INTERVAL_MS": "100",
            **(env or {}),
        })
        self.relays.append(relay)
        return relay

    def wait_workflow(self, relay, workflow_id, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, view = relay.request("GET", f"/v1/workflows/{workflow_id}")
            if view.get("status") in ("completed", "failed", "cancelled"):
                return view
            time.sleep(0.02)
        raise RuntimeError("workflow did not finish")

    def wait_step(self, relay, workflow_id, step_id, status, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, view = relay.request("GET", f"/v1/workflows/{workflow_id}")
            if steps_of(view)[step_id]["status"] == status:
                return view
            time.sleep(0.02)
        raise RuntimeError(f"{step_id} never reached {status}")

    # ----------------------------------------------------------------- the reading

    def test_reports_the_host_reading_and_what_this_host_cannot_give(self):
        relay = self.start(echo())
        status, body = relay.request("GET", "/v1/pressure")
        self.assertEqual(status, 200)
        self.assertEqual(body["verdict"], "ok")
        self.assertEqual(body["memory"], {"pressureLevel": "normal", "freePercent": 60})
        self.assertEqual(body["swap"]["usedPercent"], 5)
        self.assertIsNone(body["thermal"]["speedLimitPercent"])
        self.assertIsNone(body["temperatureC"])
        self.assertIn("powermetrics", body["temperatureReason"])
        self.assertEqual(body["guard"], {"enabled": True, "stopsAt": "critical",
                                         "holdMs": 30000})

    def test_a_warning_verdict_still_runs_work(self):
        self.set_signal("level", "1")
        relay = self.start(echo())
        _, reading = relay.request("GET", "/v1/pressure")
        self.assertEqual(reading["verdict"], "warn")
        self.assertIn("the kernel reports memory pressure warning", reading["reasons"])
        status, submitted = relay.request("POST", "/v1/workflows", {
            "steps": [{"id": "only", "agentId": "echo", "input": "still runs"}]})
        self.assertEqual(status, 202)
        done = self.wait_workflow(relay, submitted["id"])
        self.assertEqual(done["status"], "completed")

    def test_verdict_reads_swap_and_a_thermal_throttle(self):
        original = dict(os.environ)
        os.environ["AGENT_RELAY_SYSCTL"] = os.path.join(self.stubs, "sysctl")
        os.environ["AGENT_RELAY_PMSET"] = os.path.join(self.stubs, "pmset")
        try:
            self.set_signal("swap", "total = 2048.00M  used = 2000.00M  free = 48.00M  (encrypted)")
            reading = pressure.snapshot()
            self.assertEqual(reading["verdict"], "critical")
            self.assertIn("swap is 98% used", reading["reasons"])

            self.set_signal("swap", SWAP_IDLE)
            self.set_signal("therm", "CPU_Speed_Limit = 30")
            reading = pressure.snapshot()
            self.assertEqual(reading["verdict"], "critical")
            self.assertEqual(reading["thermal"]["speedLimitPercent"], 30)
            self.assertIn("CPU is throttled to 30% of its speed", reading["reasons"])
        finally:
            os.environ.clear()
            os.environ.update(original)

    def test_a_missing_probe_is_null_rather_than_a_crash(self):
        original = dict(os.environ)
        os.environ["AGENT_RELAY_SYSCTL"] = os.path.join(self.dir, "nothing-here", "sysctl")
        os.environ["AGENT_RELAY_PMSET"] = os.path.join(self.dir, "nothing-here", "pmset")
        try:
            reading = pressure.snapshot()
            self.assertEqual(reading["verdict"], "ok")
            self.assertIsNone(reading["memory"]["pressureLevel"])
            self.assertIsNone(reading["swap"]["usedBytes"])
            self.assertIsNone(reading["thermal"]["speedLimitPercent"])
        finally:
            os.environ.clear()
            os.environ.update(original)

    def test_a_probe_planted_on_PATH_is_never_run(self):
        # A sandboxed task may write outside the trees the relay protects, so a sysctl it left on
        # PATH must not be the program the relay runs.
        self.set_signal("swap", "total = 2048.00M  used = 2000.00M  free = 48.00M  (encrypted)")
        original = dict(os.environ)
        os.environ["PATH"] = f"{self.stubs}:{original.get('PATH', '')}"
        os.environ.pop("AGENT_RELAY_SYSCTL", None)
        os.environ.pop("AGENT_RELAY_PMSET", None)
        try:
            before = self.probe_calls()
            pressure.snapshot()
            self.assertEqual(self.probe_calls(), before, "a probe on PATH was executed")
        finally:
            os.environ.clear()
            os.environ.update(original)

    # ---------------------------------------------------------------- the preflight

    def test_refuses_to_start_a_workflow_when_the_host_is_critical(self):
        self.set_signal("level", "2")
        relay = self.start(echo())
        status, body = relay.request("POST", "/v1/workflows", {
            "steps": [{"id": "only", "agentId": "echo", "input": "never runs"}]})
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "device_pressure")
        self.assertIn("the kernel reports critical memory pressure", body["message"])
        self.assertEqual(body["pressure"]["verdict"], "critical")
        _, listing = relay.request("GET", "/v1/workflows")
        self.assertEqual(listing["workflows"], [])

    def test_replaying_a_request_id_is_answered_even_when_the_host_is_critical(self):
        relay = self.start(echo())
        spec = {"requestId": "replay-1",
                "steps": [{"id": "only", "agentId": "echo", "input": "kept"}]}
        status, submitted = relay.request("POST", "/v1/workflows", spec)
        self.assertEqual(status, 202)
        self.assertEqual(self.wait_workflow(relay, submitted["id"])["status"], "completed")

        self.set_signal("level", "2")
        status, again = relay.request("POST", "/v1/workflows", spec)
        # The run exists, so the replay gets it back; a refusal here would lose its id for good.
        self.assertEqual(status, 200)
        self.assertEqual(again["id"], submitted["id"])
        self.assertEqual(again["status"], "completed")

    # -------------------------------------------------------------- the kill button

    def test_kill_stops_the_run_and_a_resume_finishes_only_the_lost_step(self):
        mark = os.path.join(self.dir, "mark")
        relay = self.start([
            echo(),
            {"id": "slow", "command": PYTHON, "args": ["-c", SLOW_ONCE],
             "env": {"WF_MARK": mark}},
        ])
        _, submitted = relay.request("POST", "/v1/workflows", {"steps": [
            {"id": "first", "agentId": "echo", "input": "kept"},
            {"id": "second", "agentId": "slow", "needs": ["first"], "input": "long job"},
        ]})
        workflow_id = submitted["id"]
        self.wait_step(relay, workflow_id, "second", "running")
        # Wait for the child itself: the engine marks a step running before it spawns the process,
        # so a kill sent on the status alone can land before the child writes its mark, and the
        # resumed run would sleep again instead of finishing.
        deadline = time.time() + 45  # a loaded runner may take a while to start the interpreter
        while not os.path.exists(mark):
            if time.time() > deadline:
                self.fail("the slow step never wrote its mark")
            time.sleep(0.02)

        status, killed = relay.request("POST", "/v1/pressure/kill")
        self.assertEqual(status, 200)
        self.assertEqual(killed["killed"], [workflow_id])
        self.assertTrue(killed["reason"].startswith("device_pressure: "), killed["reason"])

        stopped = self.wait_workflow(relay, workflow_id)
        self.assertEqual(stopped["status"], "cancelled")
        self.assertTrue(stopped["error"].startswith("device_pressure: "))
        finished = steps_of(stopped)
        self.assertEqual(finished["first"]["status"], "completed")
        self.assertEqual(finished["first"]["output"], "kept")
        self.assertEqual(finished["second"]["status"], "cancelled")

        _, tasks = relay.request("GET", "/v1/tasks")
        statuses = {task["id"]: task["status"] for task in tasks["tasks"]}
        # The step that finished keeps its result; only the running one was killed.
        self.assertEqual(statuses[finished["first"]["taskIds"][0]], "completed")
        self.assertEqual(statuses[finished["second"]["taskIds"][0]], "cancelled")

        status, _ = relay.request("POST", f"/v1/workflows/{workflow_id}/resume")
        self.assertEqual(status, 202)
        done = self.wait_workflow(relay, workflow_id)
        self.assertEqual(done["status"], "completed")
        resumed = steps_of(done)
        self.assertEqual(resumed["first"]["attempts"], 1)
        self.assertEqual(resumed["first"]["taskIds"], [])
        self.assertEqual(resumed["first"]["output"], "kept")
        self.assertEqual(resumed["second"]["output"], "finished")

    def test_kill_reports_an_empty_list_when_nothing_is_running(self):
        relay = self.start(echo())
        status, body = relay.request("POST", "/v1/pressure/kill")
        self.assertEqual(status, 200)
        self.assertEqual(body["killed"], [])

    # ------------------------------------------------------------------- the guard

    def test_the_guard_stops_a_running_workflow_when_pressure_turns_critical(self):
        # A hold of 0 is the stop-at-once rule: the first critical reading ends the run, and the
        # message still says how long pressure held and which reading decided it.
        relay = self.start({"id": "slow", "command": PYTHON, "args": ["-c", SLOW]},
                           env={"AGENT_RELAY_PRESSURE_HOLD_MS": "0"})
        _, submitted = relay.request("POST", "/v1/workflows", {
            "steps": [{"id": "wait", "agentId": "slow", "input": "long job"}]})
        workflow_id = submitted["id"]
        self.wait_step(relay, workflow_id, "wait", "running")

        self.set_signal("level", "2")
        stopped = self.wait_workflow(relay, workflow_id, timeout=15)
        self.assertEqual(stopped["status"], "cancelled")
        self.assertTrue(stopped["error"].startswith("device_pressure: "), stopped["error"])
        self.assertIn("the kernel reports critical memory pressure", stopped["error"])
        held = re.search(r"critical for (\d+\.\d+)s across 1 reading; decided by ", stopped["error"])
        self.assertIsNotNone(held, stopped["error"])
        self.assertLess(float(held.group(1)), 1.0, stopped["error"])
        self.assertRegex(stopped["error"], r"decided by \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

    def test_one_critical_reading_between_normal_ones_leaves_the_run_going(self):
        # The reading that cost a long run its work in progress: one critical sample between two
        # normal ones is a spike, not a state, so the run must keep running.
        relay = self.start({"id": "slow", "command": PYTHON, "args": ["-c", SLOW]},
                           env={"AGENT_RELAY_PRESSURE_INTERVAL_MS": "500"})
        _, submitted = relay.request("POST", "/v1/workflows", {
            "steps": [{"id": "wait", "agentId": "slow", "input": "long job"}]})
        workflow_id = submitted["id"]
        self.wait_step(relay, workflow_id, "wait", "running")

        self.set_signal("level", "2")
        self.wait_level_sample("level=2")  # the guard read exactly one critical level
        self.set_signal("level", "0")
        self.wait_level_sample("level=0")  # and a normal one after it
        time.sleep(1.2)  # several more guard intervals with nothing critical to see

        _, view = relay.request("GET", f"/v1/workflows/{workflow_id}")
        self.assertEqual(view["status"], "running",
                         f"the guard stopped the run on a spike: {view.get('error')}")
        self.assertEqual(self.level_samples("level=2"), 1,
                         "the test never isolated a single critical reading")

    def test_the_guard_stops_only_after_critical_pressure_holds_for_the_hold_time(self):
        relay = self.start({"id": "slow", "command": PYTHON, "args": ["-c", SLOW]},
                           env={"AGENT_RELAY_PRESSURE_INTERVAL_MS": "100",
                                "AGENT_RELAY_PRESSURE_HOLD_MS": "400"})
        _, submitted = relay.request("POST", "/v1/workflows", {
            "steps": [{"id": "wait", "agentId": "slow", "input": "long job"}]})
        workflow_id = submitted["id"]
        self.wait_step(relay, workflow_id, "wait", "running")

        self.set_signal("level", "2")
        stopped = self.wait_workflow(relay, workflow_id, timeout=15)
        self.assertEqual(stopped["status"], "cancelled")
        error = stopped["error"]
        self.assertTrue(error.startswith("device_pressure: "), error)
        self.assertIn("the kernel reports critical memory pressure", error)
        held = re.search(r"critical for (\d+\.\d+)s", error)
        self.assertIsNotNone(held, error)
        # The whole hold, not one reading: the stop waited out the sustained critical state.
        self.assertGreaterEqual(float(held.group(1)), 0.4, error)
        readings = re.search(r"across (\d+) readings", error)
        self.assertIsNotNone(readings, error)
        self.assertGreaterEqual(int(readings.group(1)), 2, error)
        self.assertRegex(error, r"decided by \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.*"
                                r"the kernel reports critical memory pressure")

    def test_the_reading_reports_the_guard_hold_setting(self):
        # Either spelling of the setting reaches the reading; the unset default is asserted by
        # test_reports_the_host_reading_and_what_this_host_cannot_give above.
        relay = self.start(echo(), env={"AGENT_RELAY_PRESSURE_HOLD_MS": "1500"})
        _, reading = relay.request("GET", "/v1/pressure")
        self.assertEqual(reading["guard"]["holdMs"], 1500)

        relay = self.start(echo(), env={"AGENT_RELAY_PRESSURE_HOLD_MS": None,
                                        "A2A_RELAY_PRESSURE_HOLD_MS": "2500"})
        _, reading = relay.request("GET", "/v1/pressure")
        self.assertEqual(reading["guard"]["holdMs"], 2500)

    def test_a_hold_that_is_not_a_non_negative_integer_stops_the_relay(self):
        with self.assertRaises(RuntimeError) as caught:
            self.start(echo(), env={"AGENT_RELAY_PRESSURE_HOLD_MS": "-1"})
        self.assertIn("AGENT_RELAY_PRESSURE_HOLD_MS must be a non-negative integer",
                      str(caught.exception))

    def test_an_idle_relay_never_probes_the_host(self):
        relay = self.start([echo(), {"id": "slow", "command": PYTHON, "args": ["-c", SLOW]}])
        time.sleep(0.5)  # several guard intervals with nothing to guard
        self.assertEqual(self.probe_calls(), 0,
                         "the guard probed the host while no run was active")

        _, submitted = relay.request("POST", "/v1/workflows", {
            "steps": [{"id": "wait", "agentId": "slow", "input": "long job"}]})
        self.wait_step(relay, submitted["id"], "wait", "running")
        after_submit = self.probe_calls()  # the submission reads the host once
        deadline = time.time() + 10
        while time.time() < deadline and self.probe_calls() <= after_submit:
            time.sleep(0.02)
        self.assertGreater(self.probe_calls(), after_submit,
                           "the guard never sampled a running workflow")
        relay.request("DELETE", f"/v1/workflows/{submitted['id']}")

    def test_the_shared_helper_answers_from_stubs_not_the_real_host(self):
        # Every relay the suite starts reads a calm stub host, so a critical developer machine
        # cannot fail an unrelated test through the submission preflight.
        relay = Relay(echo())
        self.relays.append(relay)
        _, reading = relay.request("GET", "/v1/pressure")
        self.assertEqual(reading["verdict"], "ok")
        self.assertEqual(reading["memory"], {"pressureLevel": "normal", "freePercent": 60})

    def test_the_guard_reports_the_alias_setting(self):
        # The helper's default is unset, so the documented A2A alias alone decides.
        relay = Relay(echo(), env={
            "AGENT_RELAY_PRESSURE_GUARD": None,
            "A2A_RELAY_PRESSURE_GUARD": "0",
            "AGENT_RELAY_STATE_DIR": self.state,
        })
        self.relays.append(relay)
        _, reading = relay.request("GET", "/v1/pressure")
        self.assertFalse(reading["guard"]["enabled"])

    def test_an_empty_canonical_guard_falls_through_to_the_alias(self):
        # An empty value is not a value: it falls through the way server._env does, so the alias
        # decides. Before, an empty string meant "on" and the alias was never read.
        relay = Relay(echo(), env={
            "AGENT_RELAY_PRESSURE_GUARD": "",
            "A2A_RELAY_PRESSURE_GUARD": "0",
            "AGENT_RELAY_STATE_DIR": self.state,
        })
        self.relays.append(relay)
        _, reading = relay.request("GET", "/v1/pressure")
        self.assertFalse(reading["guard"]["enabled"])

    def test_the_guard_can_be_turned_off(self):
        relay = self.start({"id": "slow", "command": PYTHON, "args": ["-c", SLOW]},
                           env={"AGENT_RELAY_PRESSURE_GUARD": "0"})
        _, reading = relay.request("GET", "/v1/pressure")
        self.assertFalse(reading["guard"]["enabled"])
        _, submitted = relay.request("POST", "/v1/workflows", {
            "steps": [{"id": "wait", "agentId": "slow", "input": "long job"}]})
        self.wait_step(relay, submitted["id"], "wait", "running")
        self.set_signal("level", "2")
        time.sleep(0.6)  # several guard intervals
        _, view = relay.request("GET", f"/v1/workflows/{submitted['id']}")
        self.assertEqual(view["status"], "running")

    # ----------------------------------------------------------------------- MCP

    def test_mcp_reports_the_reading(self):
        relay = self.start(echo())
        client = McpClient(relay.base)
        try:
            result = client.call_tool("get_pressure", {})
            reading = result["structuredContent"]
            self.assertEqual(reading["verdict"], "ok")
        finally:
            client.close()

    def test_mcp_reports_a_refusal_as_a_tool_error(self):
        self.set_signal("level", "2")
        relay = self.start(echo())
        client = McpClient(relay.base)
        try:
            result = client.call_tool("run_workflow", {
                "spec": {"steps": [{"id": "only", "agentId": "echo", "input": "never runs"}]}})
            self.assertTrue(result["isError"])
            self.assertEqual(result["structuredContent"]["error"], "device_pressure")
            self.assertEqual(result["structuredContent"]["status"], 503)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
