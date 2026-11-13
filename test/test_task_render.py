"""A task's response body is rendered from a snapshot no writer can mutate.

The relay's rule is that a write to a live task dict holds LOCK: the worker side stamps the
start and finish fields, and every reader renders under the lock. The two directions are tested
separately. ``RacingTask`` stands in for the worker's insert landing while a poll renders the same
dict (the real window cannot be hit on demand), and ``LockGuardTask`` is the referee for the
writer's side: the dict records any write made without the lock while ``_drain`` and the worker
thread it starts run exactly as in production.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib import request as urlrequest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from relay_helpers import ROOT

sys.path.insert(0, os.path.join(ROOT, "src"))
import server


TOKEN = "render-test-token"


class RacingTask(dict):
    """The task dict the worker thread is still writing to.

    ``items()`` is what ``visible()`` walks. Handing out the view and then inserting is the same
    step order a finishing worker produces: the size changes between two ``next()`` calls, so the
    walk raises ``RuntimeError: dictionary changed size during iteration``. The insert only
    happens while LOCK is free, because that is the only time a worker thread may perform it.
    """

    def items(self):
        view = iter(super().items())

        def walk():
            yield next(view)
            if not server.LOCK._is_owned():
                self["finishedAt"] = "2026-01-01T00:00:00.000Z"
                self["output"] = "written by the worker while the poll was rendering"
            yield from view

        return walk()


def _task(task_id="racing-task", status="queued", kind=RacingTask, **fields):
    task = {
        "id": task_id,
        "sessionId": "session-1",
        "agentId": "echo",
        "input": "prompt",
        "status": status,
        "createdAt": "2026-01-01T00:00:00.000Z",
        "timeoutMs": 1000,
    }
    task.update(fields)
    return kind(**task)


class LockGuardTask(dict):
    """A task dict that records every write made without holding LOCK.

    Construction happens before ``armed``, so seeding the task is not a write the relay makes.
    After that, ``_drain`` and the worker thread it starts touch this dict exactly as they touch a
    real task's, and any mutation that did not take the lock is recorded instead of being hidden.
    """

    def __init__(self, **fields):
        super().__init__(**fields)
        self.violations = []
        self.armed = False

    def _check(self, action):
        if self.armed and not server.LOCK._is_owned():
            self.violations.append(action)

    def __setitem__(self, key, value):
        self._check("set " + str(key))
        super().__setitem__(key, value)

    def __delitem__(self, key):
        self._check("del " + str(key))
        super().__delitem__(key)

    def update(self, *args, **kwargs):
        self._check("update")
        super().update(*args, **kwargs)

    def pop(self, *args):
        self._check("pop " + str(args[0] if args else ""))
        return super().pop(*args)

    def setdefault(self, key, default=None):
        self._check("setdefault " + str(key))
        return super().setdefault(key, default)


def _dequeue(task):
    with server.LOCK:
        if task in server.QUEUE:
            server.QUEUE.remove(task)


class TaskRenderTests(unittest.TestCase):
    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.addCleanup(self.httpd.server_close)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.shutdown)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        patcher = mock.patch.object(server, "TOKEN", TOKEN)
        patcher.start()
        self.addCleanup(patcher.stop)

    def add_task(self, task):
        with server.LOCK:
            server.TASKS[task["id"]] = task
        self.addCleanup(server.TASKS.pop, task["id"], None)

    def get(self, path):
        request = urlrequest.Request(self.base + path,
                                     headers={"authorization": "Bearer " + TOKEN})
        try:
            with urlrequest.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urlrequest.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def delete(self, path):
        request = urlrequest.Request(self.base + path, method="DELETE",
                                     headers={"authorization": "Bearer " + TOKEN})
        try:
            with urlrequest.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urlrequest.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_listing_answers_from_the_snapshot_taken_under_the_lock(self):
        task = _task()
        self.add_task(task)
        status, listing = self.get("/v1/tasks")
        self.assertEqual(status, 200, listing)
        self.assertEqual([item["id"] for item in listing["tasks"]], [task["id"]])
        self.assertEqual(listing["tasks"][0]["status"], "queued")
        # The worker's insert happened after the snapshot, so this response does not carry it.
        self.assertNotIn("finishedAt", listing["tasks"][0])
        self.assertNotIn("output", listing["tasks"][0])

    def test_single_task_read_answers_from_the_snapshot_taken_under_the_lock(self):
        task = _task()
        self.add_task(task)
        status, body = self.get("/v1/tasks/" + task["id"])
        self.assertEqual(status, 200, body)
        self.assertEqual((body["id"], body["status"]), (task["id"], "queued"))
        self.assertNotIn("finishedAt", body)
        self.assertNotIn("output", body)

    def test_a_waiting_task_read_answers_from_the_snapshot_taken_under_the_lock(self):
        task = _task()
        self.add_task(task)
        status, body = self.get("/v1/tasks/%s?waitMs=50" % task["id"])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "queued")
        self.assertNotIn("finishedAt", body)

    def test_cancellation_answers_from_the_snapshot_taken_under_the_lock(self):
        task = _task()
        self.add_task(task)
        status, body = self.delete("/v1/tasks/" + task["id"])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "cancelled")
        self.assertNotIn("_finished", body)

    def test_a_worker_start_writes_the_task_dict_only_under_the_lock(self):
        task = _task(kind=LockGuardTask, timeoutMs=5000)
        self.add_task(task)
        agent = {"id": "echo", "type": "command",
                 "command": sys.executable, "args": ["-c", "print('ok')"]}
        task.armed = True
        with mock.patch.dict(server.AGENTS, {"echo": agent}), \
                mock.patch.object(server, "ACTIVE", 0):
            with server.LOCK:
                server.QUEUE.append(task)
            self.addCleanup(_dequeue, task)
            server._drain()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                with server.TASK_CONDITION:
                    if task["status"] not in ("queued", "running"):
                        break
                    server.TASK_CONDITION.wait(0.05)
        self.assertEqual(task["status"], "completed", task)
        self.assertEqual(task["output"], "ok")
        # The drain and finish fields (_deadline, _extra_args, _sandbox, startedAt, _proc,
        # finishedAt, output) were all written with LOCK held, including from the worker thread.
        self.assertEqual(task.violations, [])
        status, listing = self.get("/v1/tasks")
        self.assertEqual(status, 200, listing)
        status, body = self.get("/v1/tasks/" + task["id"])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "completed")


if __name__ == "__main__":
    unittest.main()
