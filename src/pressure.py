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

The probes are macOS-only. On another host every signal is null, the verdict is ``ok``, and the
guard never stops a run: the reading is absent rather than guessed, and a caller that needs a
verdict must not read ``ok`` as a measurement.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
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
# How long the verdict must stay critical before the guard stops a run. A spike between two
# normal readings costs a long run its work in progress, so one reading is not a state.
DEFAULT_GUARD_HOLD_MS = 30_000


# The probes run outside any sandbox, as the user a read-only task runs as, and a task may write
# anywhere outside the trees the relay protects. A sysctl or pmset found through PATH could
# therefore be one a task planted, so these are the only paths the relay will run.
TRUSTED_SYSCTL = ("/usr/sbin/sysctl", "/sbin/sysctl", "/bin/sysctl")
TRUSTED_PMSET = ("/usr/bin/pmset", "/bin/pmset")


def _probe_path(env_name, trusted):
    """The absolute path of one probe, or None. Never a PATH lookup.

    The owner may name the tool in the relay's own environment, which a task cannot change; the
    name is accepted only as an absolute path to an executable file. Otherwise the first trusted
    path that is an executable file, which on macOS is the system tool.
    """
    override = os.environ.get(env_name)
    if override is not None:
        usable = os.path.isabs(override) and os.path.isfile(override) and os.access(override, os.X_OK)
        return override if usable else None
    return next((path for path in trusted
                 if os.path.isfile(path) and os.access(path, os.X_OK)), None)


def _run(path, argv):
    """One probe's stdout, or None when the tool is missing, refuses, or hangs."""
    if path is None:
        return None
    try:
        result = subprocess.run([path] + argv, capture_output=True, text=True,
                                timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _sysctl(name):
    out = _run(_probe_path("AGENT_RELAY_SYSCTL", TRUSTED_SYSCTL), ["-n", name])
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


def guard_enabled():
    """Whether the relay stops a run on its own, read from the relay's own environment.

    The one implementation of the rule: ``server`` starts the guard thread from this answer and
    the reading reports it, so the two cannot disagree. Either spelling counts, an empty value
    falls through to the alias, and no value at all means on.
    """
    for name in ("AGENT_RELAY_PRESSURE_GUARD", "A2A_RELAY_PRESSURE_GUARD"):
        value = os.environ.get(name)
        if value:
            return value.lower() not in ("0", "false", "no", "off")
    return True


def guard_hold_ms():
    """How long the verdict must stay critical before the guard stops a run, in milliseconds.

    The one implementation of the setting: the guard thread stops from this answer and the
    reading reports it, so the two cannot disagree. Either spelling counts, an empty value
    falls through to the alias, and no value at all means ``DEFAULT_GUARD_HOLD_MS``. ``0``
    keeps the stop-at-once rule; a value that is not a non-negative integer raises
    ``ValueError``, which the relay reports at startup instead of guessing.
    """
    for name in ("AGENT_RELAY_PRESSURE_HOLD_MS", "A2A_RELAY_PRESSURE_HOLD_MS"):
        value = os.environ.get(name)
        if value:
            hold = _as_int(value)
            if hold is None or hold < 0:
                raise ValueError(f"{name} must be a non-negative integer")
            return hold
    return DEFAULT_GUARD_HOLD_MS


class CriticalHold:
    """How long critical pressure has held, and the readings that prove it.

    The guard feeds it one reading at a time: a critical verdict joins the streak and anything
    else clears it, so one critical reading between two normal ones never stops a run. The
    streak stops a run once it has held the whole hold time and has been read at least twice —
    a second reading is what separates a state from a spike — except that a hold of 0 keeps the
    stop-at-once rule and stops on the first reading. The stop message carries the duration and
    every reading that counted, so a stopped run says why rather than only that.
    """

    def __init__(self, hold_ms):
        self.hold_ms = hold_ms
        self.readings = []
        self._started = None

    def reset(self):
        """Forget the streak, so the next critical reading starts a new one."""
        self.readings = []
        self._started = None

    def sample(self, reading, now=None):
        """Take one host reading into the streak; a non-critical verdict clears it."""
        if reading["verdict"] != "critical":
            self.reset()
            return
        if not self.readings:
            self._started = time.monotonic() if now is None else now
        self.readings.append(reading)

    def should_stop(self, now=None):
        """Whether critical pressure has held long enough to stop every active run."""
        if not self.readings:
            return False
        if self.hold_ms and len(self.readings) < 2:
            return False
        now = time.monotonic() if now is None else now
        return now - self._started >= self.hold_ms / 1000.0

    def message(self, now=None):
        """The stop reason: how long pressure held and the readings that decided it.

        Call only once ``should_stop`` said yes: there is no duration to report without a streak.
        """
        held = (time.monotonic() if now is None else now) - self._started
        decided_by = " | ".join(f"{reading['sampledAt']} {', '.join(reading['reasons'])}"
                                for reading in self.readings)
        count = len(self.readings)
        plural = "reading" if count == 1 else "readings"
        return (f"device_pressure: critical for {held:.1f}s across {count} {plural}; "
                f"decided by {decided_by}")


def snapshot():
    """One reading of this host, with a verdict a caller can act on."""
    level = PRESSURE_LEVELS.get(_as_int(_sysctl("vm.memory_pressure")))
    free_percent = _as_int(_sysctl("kern.memorystatus_level"))
    used, total = _parse_swap(_run(_probe_path("AGENT_RELAY_SYSCTL", TRUSTED_SYSCTL),
                                   ["-n", "vm.swapusage"]))
    speed_limit = _parse_speed_limit(_run(_probe_path("AGENT_RELAY_PMSET", TRUSTED_PMSET),
                                          ["-g", "therm"]))
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
        "guard": {"enabled": guard_enabled(), "stopsAt": "critical",
                  "holdMs": guard_hold_ms()},
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
