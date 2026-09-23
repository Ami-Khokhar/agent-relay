#!/usr/bin/env python3
"""Fake relay.adapter/v1 stdio adapter used by the test suite."""
from __future__ import annotations

import json
import os
import sys


def main():
    mode = os.environ.get("FAKE_ADAPTER_MODE")
    if mode == "early-exit":
        return
    request = json.loads(sys.stdin.read())
    task = request["task"]
    if mode == "malformed":
        sys.stdout.write("not json")
    elif mode == "failed":
        sys.stdout.write(json.dumps(
            {"protocolVersion": "relay.adapter/v1", "status": "failed", "error": "fake failure"}))
    elif mode == "oversized":
        sys.stdout.write(json.dumps(
            {"protocolVersion": "relay.adapter/v1", "status": "completed", "output": "x" * 10000}))
    else:
        sys.stdout.write(json.dumps(
            {"protocolVersion": "relay.adapter/v1", "status": "completed",
             "output": f"{task['sessionId']}:{task['input']}"}))


if __name__ == "__main__":
    main()
