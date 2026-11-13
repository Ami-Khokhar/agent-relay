"""A task's response body is rendered from a snapshot no worker thread can mutate.

The real window — the worker thread inserting startedAt while a poll renders the same dict, or
_finish inserting finishedAt/output — cannot be hit on demand, so these tests stand in for it with
a task dict whose iteration mutates it, exactly as the worker would. What the mapping needs to
decide that is the relay's own rule: a worker inserts only while LOCK is free, so a render that
holds LOCK sees a still task and a render that released it walks a dict that changes underneath.
"""
from __future__ import annotations

import json
import os
import sys
import threading
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


def _task(task_id="racing-task", status="queued"):
    task = RacingTask(
        id=task_id,
        sessionId="session-1",
        agentId="echo",
        input="prompt",
        status=status,
        createdAt="2026-01-01T00:00:00.000Z",
        timeoutMs=1000,
    )
    return task


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


if __name__ == "__main__":
    unittest.main()
