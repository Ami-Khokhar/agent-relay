#!/usr/bin/env python3
"""Example relay.adapter/v1 stdio adapter.

Replace run_harness with the supported API or CLI invocation for the harness being
adapted. The adapter reads one request document from stdin, writes one result document
to stdout, and sends diagnostics to stderr.
"""
from __future__ import annotations

import json
import sys


def run_harness(prompt):
    return f"example harness received: {prompt}"


def main():
    raw = sys.stdin.read()
    try:
        request = json.loads(raw)
        task = request.get("task") if isinstance(request, dict) else None
        if request.get("protocolVersion") != "relay.adapter/v1" or not isinstance(task, dict) \
                or not isinstance(task.get("input"), str):
            raise ValueError("invalid relay.adapter/v1 request")
        output = run_harness(task["input"])
        result = {"protocolVersion": "relay.adapter/v1", "status": "completed", "output": output}
    except Exception as exc:  # noqa: BLE001 - report any failure as a relay.adapter/v1 result
        result = {"protocolVersion": "relay.adapter/v1", "status": "failed", "error": str(exc)}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
