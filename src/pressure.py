"""Host pressure: how hard this machine is working, and whether a workflow should start.

Three unprivileged signals come from macOS itself:

- the kernel's memory pressure level (``sysctl vm.memory_pressure``) and the free-memory
  percentage (``sysctl kern.memorystatus_level``, the same number ``memory_pressure -Q``
  prints);
- swap in use (``sysctl vm.swapusage``);
- any thermal throttle the OS has recorded (``pmset -g therm``).

The verdict is decided from those readings rather than from a single opaque flag, so every
verdict can name the number that produced it. CPU temperature in degrees is not readable
without root on Apple Silicon: ``powermetrics`` refuses a non-root caller and macOS exposes
no SMC sensor node, so ``temperatureC`` is null with the reason attached rather than guessed.
A missing thermal reading from ``pmset`` means the OS has recorded no throttle, which is the
ordinary state on a cool machine.
"""
from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone

PROBE_TIMEOUT_S = 2
# vm.memory_pressure: what the kernel currently says about memory (vm_pageout.h).
PRESSURE_LEVELS = {0: "normal", 1: "warning", 2: "critical"}
# Verdict thresholds. Critical is the only level that stops work on its own.
CRITICAL_FREE_PERCENT = 4
CRITICAL_SWAP_PERCENT = 90
CRITICAL_SPEED_LIMIT = 40
WARN_FREE_PERCENT = 12
WARN_SWAP_PERCENT = 70
WARN_SPEED_LIMIT = 75
TEMPERATURE_REASON = ("macOS exposes no unprivileged CPU temperature; powermetrics needs root")


def _run(argv):
    """One probe's stdout, or None when the tool is missing, refuses, or hangs.

    PATH lookup is deliberate: these tools only report, and it lets a test stand a stub in
    front of them, which is how the pressure paths are exercised without a busy machine.
    """
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _sysctl(name):
    out = _run(["sysctl", "-n", name])
    return out.strip() if out else None


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_UNITS = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}
_SWAP_RE = re.compile(r"(total|used|free)\s*=\s*([\d.]+)([KMG])")


def _parse_swap(text):
    total = used = None
    for name, number, unit in _SWAP_RE.findall(text or ""):
        value = int(float(number) * _UNITS[unit])
        if name == "total":
            total = value
        elif name == "used":
            used = value
    return used, total


def _parse_speed_limit(text):
    match = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", text or "")
    return int(match.group(1)) if match else None


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def snapshot():
    """One reading of this host, with a verdict a caller can act on."""
    level = PRESSURE_LEVELS.get(_as_int(_sysctl("vm.memory_pressure")))
    free_percent = _as_int(_sysctl("kern.memorystatus_level"))
    used, total = _parse_swap(_run(["sysctl", "-n", "vm.swapusage"]))
    speed_limit = _parse_speed_limit(_run(["pmset", "-g", "therm"]))
    swap_percent = round(used * 100 / total) if used is not None and total else None

    critical, warn = [], []
    if level == "critical":
        critical.append("the kernel reports critical memory pressure")
    elif level == "warning":
        warn.append("the kernel reports memory pressure warning")
    if free_percent is not None:
        if free_percent < CRITICAL_FREE_PERCENT:
            critical.append(f"{free_percent}% of memory is free")
        elif free_percent < WARN_FREE_PERCENT:
            warn.append(f"{free_percent}% of memory is free")
    if swap_percent is not None:
        if swap_percent >= CRITICAL_SWAP_PERCENT:
            critical.append(f"swap is {swap_percent}% used")
        elif swap_percent >= WARN_SWAP_PERCENT:
            warn.append(f"swap is {swap_percent}% used")
    if speed_limit is not None:
        if speed_limit < CRITICAL_SPEED_LIMIT:
            critical.append(f"CPU is throttled to {speed_limit}% of its speed")
        elif speed_limit < WARN_SPEED_LIMIT:
            warn.append(f"CPU is throttled to {speed_limit}% of its speed")

    verdict = "critical" if critical else ("warn" if warn else "ok")
    return {
        "verdict": verdict,
        "reasons": critical or warn,
        "memory": {"pressureLevel": level, "freePercent": free_percent},
        "swap": {"usedBytes": used, "totalBytes": total, "usedPercent": swap_percent},
        "thermal": {"speedLimitPercent": speed_limit},
        "temperatureC": None,
        "temperatureReason": TEMPERATURE_REASON,
        "sampledAt": _now_iso(),
        "guard": {
            "enabled": os.environ.get("AGENT_RELAY_PRESSURE_GUARD", "1").lower()
            not in ("0", "false", "no", "off"),
            "stopsAt": "critical",
        },
    }


def should_refuse(reading):
    """The refusal body for a run that must not start, or None."""
    if reading["verdict"] != "critical":
        return None
    return {
        "error": "device_pressure",
        "message": "This machine is under critical pressure, so no workflow was started: "
                   + "; ".join(reading["reasons"]) + ". Free some memory and retry; "
                   "GET /v1/pressure shows the current reading.",
        "pressure": reading,
    }
