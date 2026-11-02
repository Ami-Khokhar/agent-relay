#!/usr/bin/env python3
"""The restricted per-user service boundary for activation (#119, #159).

Activation's stop/restart boundary speaks to only two positively validated
per-user service definitions: a macOS launchd job ``com.agent-relay``, or a
systemd --user unit ``agent-relay.service`` — and only those definitions
whose program is this installation's own ``src/server.py`` under this
installer's checkout, with a clean interpreter and root. Nothing else is
supported: no installer, no main-pull flow, no force mode, no hooks, no
arbitrary command or service selector, no sudo and no rewriting of a host
service configuration. Original definitions, PATH, port and configuration
are preserved byte for byte; activation only restarts them, and only after
the updater's caller holds the server-acknowledged quiescence, so no active
work is ever interrupted.

``inspect_service`` reads definitions safely (no symlink, private to this
user, bounded size, strictly parsed), rejects foreign, missing, ambiguous
or weakened definitions, and refuses configuration that lives inside the
checkout, so activation can never rewrite or execute it. ``restart_service``
only runs the plain per-user manager verbs (``bootout``/``bootstrap`` for
launchd, ``restart`` for systemd --user) through fixed absolute executables
with a scrubbed environment — never as root, never with extra arguments.
``await_revision`` polls an authenticated endpoint the caller wired until it
reports exactly the expected full commit: an unknown health answer is an
immediate refusal, and the deadline is hard.
"""

from __future__ import annotations

import configparser
import hashlib
import os
import plistlib
import shlex
import stat
import subprocess
import time
from pathlib import Path

from release_identity import UpdateError
import update_staging

SERVICE_LABEL = "com.agent-relay"
SYSTEMD_SERVICE = "agent-relay.service"
PLIST_DIRNAME = Path("Library") / "LaunchAgents"
UNIT_DIRNAME = Path(".config") / "systemd" / "user"
SCRIPT_PARTS = ("src", "server.py")
MAX_SERVICE_BYTES = 64 * 1024
RESTART_TIMEOUT_S = 30
POLL_S = 0.2
SHELL_NAMES = frozenset(("sh", "bash", "dash", "zsh", "ksh", "csh", "tcsh", "fish"))
# Only these top-level plist keys describe a supported definition; anything
# else could carry paths or hooks we have not positively validated.
PLIST_KEYS = frozenset(("Label", "Program", "ProgramArguments", "WorkingDirectory",
                        "EnvironmentVariables", "RunAtLoad", "KeepAlive",
                        "StandardOutPath", "StandardErrorPath", "ProcessType"))
SYSTEMD_KEYS = frozenset(("ExecStart", "WorkingDirectory", "Environment", "Restart",
                          "RestartSec", "Type"))
SYSTEMD_FORBIDDEN = frozenset(("EnvironmentFile", "ExecStartPre", "ExecStartPost",
                              "ExecStop", "ExecStopPost", "ExecReload"))
MANAGER_TIMEOUT_S = 30
# How long the restarted relay may take to report the exact activated commit.
AWAIT_REVISION_S = 120.0


class ServiceError(UpdateError):
    """A service definition could not be trusted or a restart was refused."""


def _commit_re():
    """The exact 40-hex commit shape update_transaction defines."""
    import update_transaction
    return update_transaction.COMMIT_RE


def _euid():
    owner = getattr(os, "geteuid", None)
    return None if owner is None else owner()


def _plain_definition(path, label):
    """A regular, user-owned definition file: no symlink, no group/world write."""
    path = Path(path)
    try:
        info = os.lstat(str(path))
    except OSError as exc:
        raise ServiceError("%s service definition is missing or unreadable; refusing "
                           "(%s)" % (label, exc)) from exc
    if not stat.S_ISREG(info.st_mode) or os.path.islink(str(path)):
        raise ServiceError("%s service definition is not a plain regular file; refusing "
                           "a symlinked or special definition" % label)
    owner = _euid()
    if owner is not None and info.st_uid != owner:
        raise ServiceError("%s service definition is not owned by this user; refusing a "
                           "foreign definition" % label)
    if info.st_mode & 0o022:
        raise ServiceError("%s service definition is group- or world-writable; refusing "
                           "an unsafe definition" % label)
    try:
        if os.path.realpath(str(path)) != str(path.resolve(strict=False)) or \
                len(path.read_bytes()) > MAX_SERVICE_BYTES:
            raise ServiceError("%s service definition is unreachable or oversized; "
                               "refusing" % label)
    except OSError as exc:
        raise ServiceError("cannot read the %s service definition: %s" % (label, exc)) \
            from exc
    return path


def _analysis(args, install_root, label):
    """The interpreter and the one supported script an argument list must name."""
    if not isinstance(args, list) or not args or \
            not all(isinstance(arg, str) and arg for arg in args):
        raise ServiceError("%s service names no plain program arguments; refusing an "
                           "ambiguous definition" % label)
    for arg in args:
        low = os.path.basename(arg).lower()
        if low in SHELL_NAMES or "sudo" in arg or arg == "-c":
            raise ServiceError("%s service routes through %r; refusing a shell or "
                               "privileged definition" % (label, arg))
    resolved_root = str(Path(install_root).resolve())
    script = None
    for index, arg in enumerate(args):
        path = Path(arg)
        if not path.is_absolute():
            continue
        try:
            resolved = path.resolve(strict=False)
        except OSError:
            continue
        if resolved == Path(resolved_root, *SCRIPT_PARTS) and \
                str(resolved).startswith(resolved_root + os.sep):
            if script is not None:
                raise ServiceError("%s service names the relay script twice; refusing "
                                   "an ambiguous definition" % label)
            script = (index, resolved)
    if script is None:
        raise ServiceError("%s service does not run this installation's %s; refusing a "
                           "foreign or unsupported definition"
                           % (label, os.sep.join(SCRIPT_PARTS)))
    index, resolved = script
    interpreter = None
    if index > 0:
        candidate = Path(args[index - 1])
        if not candidate.is_absolute():
            raise ServiceError("%s service uses a relative interpreter %r; refusing an "
                               "ambiguous definition" % (label, args[index - 1]))
        try:
            # a plain stat: interpreters are frequently shipped as symlinks to
            # a versioned binary; the target must be an executable real file
            info = os.stat(str(candidate))
        except OSError as exc:
            raise ServiceError("cannot inspect the service interpreter %s: %s"
                               % (candidate, exc)) from exc
        if not (stat.S_ISREG(info.st_mode)
                and "python" in os.path.basename(str(candidate)).lower()):
            raise ServiceError("%s service does not run the relay through a Python "
                               "interpreter; refusing an unsupported definition" % label)
        if not os.access(str(candidate), os.X_OK):
            raise ServiceError("%s service interpreter %s is not executable; refusing"
                               % (label, candidate))
        if str(candidate.resolve(strict=False)).startswith(resolved_root + os.sep):
            raise ServiceError("%s service interpreter lives inside the checkout; "
                               "refusing" % label)
        interpreter = str(candidate.resolve(strict=False))
    for name, value in _path_arguments(args):
        resolved = Path(value).resolve(strict=False)
        if str(resolved).startswith(resolved_root + os.sep):
            raise ServiceError("%s service configuration %s=%s lives inside the checkout; "
                               "activation must never touch configuration, so external "
                               "configuration is required" % (label, name, value))
    if index + 1 < len(args):
        raise ServiceError("%s service passes extra arguments after the relay script "
                           "%r; refusing an ambiguous definition" % (label, args[index + 1:]))
    return {"interpreter": interpreter,
            "script": str(Path(install_root, *SCRIPT_PARTS).resolve(strict=False))}


def _path_arguments(args):
    """Every (name, value) pair the argument list could use for a configuration path."""
    pairs = []
    for index, arg in enumerate(args):
        for flag in ("--config", "-c"):
            if arg == flag and index + 1 < len(args):
                pairs.append(("--config", args[index + 1]))
        if arg.startswith("--config="):
            pairs.append(("--config", arg.split("=", 1)[1]))
    return pairs


def _working_directory(value, resolved_root, label):
    if value is None or str(value).rstrip("/") != resolved_root.rstrip("/"):
        raise ServiceError("%s service does not run under this install dir; refusing a "
                           "foreign definition" % label)


def _plist(path, resolved_root, label):
    try:
        with open(str(path), "rb") as handle:
            definition = plistlib.load(handle)
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        raise ServiceError("the %s service definition is not a readable property "
                           "list: %s" % (label, exc)) from exc
    if not isinstance(definition, dict) or definition.get("Label") != SERVICE_LABEL:
        raise ServiceError("the %s service definition is not %s or names no such "
                           "label; refusing an ambiguous definition" % (label, SERVICE_LABEL))
    unexpected = set(definition) - PLIST_KEYS
    if unexpected:
        raise ServiceError("the %s service definition carries unsupported keys %s; "
                           "refusing" % (label, ", ".join(sorted(unexpected))))
    program = definition.get("ProgramArguments")
    if program is None and definition.get("Program") is not None:
        program = [definition["Program"]]
    _analysis(program or [], resolved_root, label)
    _working_directory(definition.get("WorkingDirectory"), resolved_root, label)
    environment = definition.get("EnvironmentVariables") or {}
    if not isinstance(environment, dict) or \
            not all(isinstance(key, str) and isinstance(value, str)
                    for key, value in environment.items()):
        raise ServiceError("the %s service definition has unreadable environment "
                           "variables; refusing" % label)
    for key, value in environment.items():
        if "PATH" in key.upper() or "CONFIG" in key.upper() or "RELAY" in key.upper():
            # these are preserved, never rewritten; they must not hide another
            # checkout inside this own install dir
            resolved = Path(value).resolve(strict=False) if os.path.isabs(value) else None
            if resolved is not None and str(resolved).startswith(str(resolved_root) + os.sep):
                raise ServiceError("the %s service preserves environment %s=%s inside "
                                   "the checkout; refusing" % (label, key, value))
    return {"kind": "launchd", "label": SERVICE_LABEL, "path": str(path),
            "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
            "programArguments": list(program or []),
            "environment": {key: value for key, value in environment.items()}}


def _systemd(path, resolved_root, label):
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        with open(str(path), encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error, UnicodeDecodeError) as exc:
        raise ServiceError("the %s service definition is not a readable systemd unit: "
                           "%s" % (label, exc)) from exc
    if not parser.has_section("Service"):
        raise ServiceError("the %s service definition has no [Service] section; "
                           "refusing" % label)
    service = {key.lower(): value for key, value in parser.items("Service")}
    unexpected = {key.lower() for key in service} - {key.lower() for key in SYSTEMD_KEYS}
    if unexpected:
        raise ServiceError("the %s service definition carries unsupported directives %s; "
                           "refusing" % (label, ", ".join(sorted(unexpected))))
    forbidden = {key.lower() for key in service} & {key.lower() for key in SYSTEMD_FORBIDDEN}
    if forbidden:
        raise ServiceError("the %s service definition carries forbidden directives %s "
                           "(arbitrary commands or external configuration); refusing"
                           % (label, ", ".join(sorted(forbidden))))
    if "execstart" not in service or "workingdirectory" not in service:
        raise ServiceError("the %s service definition names no ExecStart and "
                           "WorkingDirectory; refusing an ambiguous definition" % label)
    try:
        args = shlex.split(service.get("execstart") or service.get("ExecStart", ""),
                           comments=False, posix=True)
    except ValueError as exc:
        raise ServiceError("the %s service ExecStart cannot be parsed: %s"
                           % (label, exc)) from exc
    if args and args[0][:1] in ("-", "+", "@"):
        raise ServiceError("the %s service ExecStart carries modifiers %r; refusing"
                           % (label, args[0][:1]))
    _analysis(args, resolved_root, label)
    _working_directory(service.get("workingdirectory"), resolved_root, label)
    for assignment in service.get("environment", "").split():
        name, _, value = assignment.partition("=")
        resolved = Path(value).resolve(strict=False) if os.path.isabs(value) else None
        if resolved is not None and str(resolved).startswith(str(resolved_root) + os.sep):
            raise ServiceError("the %s service preserves environment %s=%s inside the "
                               "checkout; refusing" % (label, name, value))
    return {"kind": "systemd", "label": SYSTEMD_SERVICE, "path": str(path),
            "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
            "programArguments": args,
            "environment": service.get("Environment", "")}


def inspect_service(install_root):
    """Identify the one supported per-user service definition for this installation.

    Returns a receipt with the definition's kind, path, exact program arguments
    and environment (preserved, never rewritten). Refuses when no definition
    exists, when both manager definitions exist (ambiguous), and every
    definition that is missing, symlinking, foreign, weakened, or runs anything
    but this checkout's own ``src/server.py`` under ``install_root``.
    """
    resolved_root = str(Path(install_root).resolve())
    try:
        update_staging._plain_private_dir(resolved_root, "install dir")
    except update_staging.StagingError as exc:
        raise ServiceError(str(exc)) from exc
    home = Path.home()
    candidates = []
    plist_path = home / PLIST_DIRNAME / ("%s.plist" % SERVICE_LABEL)
    unit_path = home / UNIT_DIRNAME / SYSTEMD_SERVICE
    for path, label, parser in ((plist_path, "launchd", _plist),
                                (unit_path, "systemd --user", _systemd)):
        if path.exists():
            candidates.append((path, label, parser))
    if not candidates:
        raise ServiceError("no per-user %s or %s service definition at %s or %s; only "
                           "positively validated per-user services are supported"
                           % (SERVICE_LABEL, SYSTEMD_SERVICE, plist_path, unit_path))
    if len(candidates) > 1:
        raise ServiceError("both launchd and systemd service definitions exist; refusing "
                           "an ambiguous service boundary")
    path, label, parser = candidates[0]
    _plain_definition(path, label)
    return parser(path, resolved_root, label)


def _manager_env(extra=()):
    """An environment built from nothing plus the variables a user service bus needs."""
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    if os.environ.get("HOME"):
        env["HOME"] = os.environ["HOME"]
    for name in extra:
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def _manager_executable(names, label):
    for candidate in names:
        if os.path.isabs(candidate) and os.path.isfile(candidate) \
                and os.access(candidate, os.X_OK):
            return candidate
    raise ServiceError("no absolute %s executable found; refusing a PATH-resolved "
                       "service manager" % label)


def restart_service(service):
    """Restart the per-user service with the plain manager verbs — nothing more.

    Acts only once the caller holds server-acknowledged quiescence, so no
    active work is ever force-killed: a plain stop/start under a held
    quiescence is the whole boundary. Refuses to run as root, uses only fixed
    absolute executables, a scrubbed environment and the original definition,
    and never runs an installer, a pull, sudo, or any kill/force control.
    """
    if _euid() == 0:
        raise ServiceError("relay restart was ordered as root; per-user services are "
                           "run by the login user, never root")
    if not isinstance(service, dict) or service.get("kind") not in ("launchd", "systemd"):
        raise ServiceError("restart_service needs a receipt from inspect_service")
    commands = []
    if service["kind"] == "launchd":
        launchctl = _manager_executable(("/bin/launchctl", "/usr/bin/launchctl"), "launchctl")
        uid = _euid()
        commands.append([launchctl, "bootout", "gui/%d/%s" % (uid, service["label"])])
        commands.append([launchctl, "bootstrap", "gui/%d" % uid, service["path"]])
        env = _manager_env()
    else:
        systemctl = _manager_executable(("/usr/bin/systemctl", "/bin/systemctl"), "systemctl")
        commands.append([systemctl, "--user", "restart", service["label"]])
        env = _manager_env(("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"))
    for argv in commands:
        try:
            process = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL,
                                     capture_output=True, text=True,
                                     timeout=MANAGER_TIMEOUT_S)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise ServiceError("the %s service manager could not be run: %s"
                               % (service["kind"], exc)) from exc
        if process.returncode != 0:
            raise ServiceError("the %s service manager refused %s: %s"
                               % (service["kind"], argv[0],
                                  process.stderr.strip()[:400] or process.returncode))
    return {"kind": service["kind"], "commands": commands,
            "restartedAt": time.time()}


def await_revision(expected_commit, *, authenticated_request, deadline_s=60.0,
                   poll_s=POLL_S):
    """Poll an authenticated endpoint until it reports exactly ``expected_commit``.

    ``authenticated_request`` is a caller-wired, bearer-authenticated request
    that returns the parsed JSON body (for example the relay's ``/v1/agents``).
    A transport failure keeps polling until the hard ``deadline_s`` is spent;
    a body that arrives without a well-formed full commit is an immediate
    refusal, not a guess: an unknown health answer never passes for a
    revision. Returns the last body; a successful exact revision must later be
    paired with the barrier-ownership check the caller performs.
    """
    if not isinstance(expected_commit, str) or not _commit_re().fullmatch(expected_commit):
        raise ServiceError("await_revision needs an exact 40-hex commit")
    if not callable(authenticated_request):
        raise ServiceError("await_revision needs the caller-wired authenticated request")
    if not isinstance(deadline_s, (int, float)) or deadline_s <= 0:
        raise ServiceError("await_revision needs a positive deadline")
    deadline = time.monotonic() + float(deadline_s)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ServiceError("the relay never reported the exact commit %s within "
                               "%.0fs; the activation remains blocked, run recover-abort"
                               % (expected_commit, deadline_s))
        try:
            body = authenticated_request()
        except Exception:  # transport trouble keeps waiting, bounded
            if remaining <= poll_s:
                continue
            time.sleep(poll_s)
            continue
        if not isinstance(body, dict) or not isinstance(body.get("revision"), dict) \
                or not isinstance(body["revision"].get("commit"), str) \
                or not _commit_re().fullmatch(body["revision"]["commit"]):
            raise ServiceError("the relay answered with unknown health and no exact "
                               "revision; refusing to mistake it for the activated "
                               "commit %s" % expected_commit)
        if body["revision"]["commit"] == expected_commit:
            return body
        time.sleep(min(poll_s, max(remaining, 0.0)))
