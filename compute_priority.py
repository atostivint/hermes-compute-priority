"""Core of hermes-compute-priority: find the isolated turn workers, lower their priority.

Why this exists
---------------
Hermes can run agent turns in a child process (``dashboard.turn_isolation``). That child is what
holds the GIL when a turn parses a model stream or dispatches tools. The process that serves the
Desktop's WebSocket is its **parent**, and on a small machine the parent must win.

Neither knob available in systemd does this on its own:

* ``CPUWeight`` arbitrates **between units**. The child lives in the same cgroup as the parent, so
  it gets exactly the same share.
* ``Nice`` arbitrates **between processes**, but niceness is **inherited** at fork/exec: the child
  starts at the parent's nice and stays there. Measured on a 1 vCPU host: five dashboard
  processes, two generations, all at ``-5``.

So the parent cannot outrank its own child through configuration. Raising the child's niceness
after it starts can, and that is what this module does. Only *raising* nice is used: lowering the
priority of a process we own needs no privilege, and it can never make anything more urgent than it
was.

Two modes
---------
By default the plugin knows exactly one class of process: the turn worker. ``parse_rules`` adds
opt-in classes (MCP servers, plugin hosts, anything the operator names), and each rule carries its
own target niceness. The extra rules are empty unless the operator configures them — a published
plugin must not start deprioritising parts of someone else's Hermes without being asked.

Everything here is pure and injectable: ``find_compute_hosts`` and ``find_by_rules`` read
``/proc``, and ``apply_once`` takes its scan and its setter as arguments, so tests never touch a
real process.
"""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import dataclass
from typing import Callable, Sequence

#: Default niceness given to a turn worker. ``0`` is the ordinary priority and ``19`` the lowest;
#: 10 leaves the worker clearly behind the serving process while still making progress.
DEFAULT_TARGET_NICE = 10

#: How often the worker scan runs. Two seconds bounds both the delay before a new worker is
#: reniced and the cost of the scan (one ``/proc`` listing, two files per Hermes process).
DEFAULT_INTERVAL_S = 2.0

#: Command-line fragments that identify a turn worker. The supervisor spawns
#: ``[sys.executable, "-m", "tui_gateway.compute_host"]`` (``tui_gateway/host_supervisor.py``),
#: so the module name is the stable part.
COMPUTE_HOST_MARKERS: tuple[str, ...] = ("tui_gateway.compute_host",)

#: Extra rules never ask for a *higher* priority. Renicing below zero needs a privilege this
#: plugin does not want, and a published default must never try.
MIN_RULE_NICE = 0
MAX_RULE_NICE = 19

#: A very short match matches almost any command line — ``"a"`` would hit most processes on the
#: machine. Found the hard way: a test using it reniced real processes on a CI runner.
MIN_RULE_MATCH_LEN = 3


@dataclass(frozen=True)
class Reniced:
    """One process whose niceness was raised by this process."""

    pid: int
    previous: int
    current: int


@dataclass(frozen=True)
class Rule:
    """One opt-in class of process to deprioritise.

    ``match`` is a plain substring of the command line, deliberately: the plugin may not hook
    core code, so the command line is the only stable handle on what a process is.
    """

    name: str
    match: str
    nice: int


def parse_rules(raw: str | None) -> list[Rule]:
    """Parse the operator's JSON rule list. Anything invalid is dropped, never raised.

    Expected shape::

        [{"name": "mcp", "match": "mcp-server", "nice": 15}]

    An empty or absent value returns an empty list, which is the shipped default: no extra class
    of process is touched unless someone asks for it by name.
    """
    if not raw or not str(raw).strip():
        return []

    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []

    if not isinstance(data, list):
        return []

    rules: list[Rule] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        match = str(item.get("match") or "").strip()
        try:
            nice = int(item.get("nice", DEFAULT_TARGET_NICE))
        except (TypeError, ValueError):
            continue
        if not name or not match:
            continue
        if len(match) < MIN_RULE_MATCH_LEN:
            continue
        if not MIN_RULE_NICE <= nice <= MAX_RULE_NICE:
            continue
        rules.append(Rule(name=name, match=match, nice=nice))
    return rules


def _read_cmdline(proc_root: str | pathlib.Path, pid: int) -> str:
    """The process command line, NUL-separated on disk, or ``""`` when unreadable.

    A worker can exit between the directory listing and this read; that race is normal and is
    reported as an empty command line rather than an error.
    """
    try:
        raw = (pathlib.Path(proc_root) / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def _iter_pids(proc_root: str | pathlib.Path, uid: int | None) -> list[int]:
    """Pids owned by ``uid`` (default: this user), skipping anything unreadable."""
    root = pathlib.Path(proc_root)
    if uid is None:
        uid = os.getuid() if hasattr(os, "getuid") else None

    pids: list[int] = []
    try:
        # `iterdir()` is a generator: a missing or unreadable root raises while iterating, not
        # when it is called, so the listing has to be materialised inside the guard.
        entries = list(root.iterdir())
    except OSError:
        return pids

    for entry in entries:
        if not entry.name.isdigit():
            continue
        if uid is not None:
            try:
                if entry.stat().st_uid != uid:
                    continue
            except OSError:
                continue
        pids.append(int(entry.name))
    return pids


def find_compute_hosts(
    proc_root: str | pathlib.Path = "/proc",
    uid: int | None = None,
    markers: Sequence[str] = COMPUTE_HOST_MARKERS,
) -> list[int]:
    """Pids of turn workers owned by ``uid`` (default: this user).

    Only same-user processes are returned, because that is exactly the set this process is
    allowed to renice. Another user's worker stays untouched — with a minimal setup it is not ours
    to slow down.
    """
    found: list[int] = []
    for pid in _iter_pids(proc_root, uid):
        command = _read_cmdline(proc_root, pid)
        if command and any(marker in command for marker in markers):
            found.append(pid)
    return found


def find_by_rules(
    rules: Sequence[Rule],
    proc_root: str | pathlib.Path = "/proc",
    uid: int | None = None,
) -> list[tuple[int, Rule]]:
    """Pids matching an operator rule, first matching rule in declaration order wins."""
    matches: list[tuple[int, Rule]] = []
    if not rules:
        return matches
    for pid in _iter_pids(proc_root, uid):
        command = _read_cmdline(proc_root, pid)
        if not command:
            continue
        for rule in rules:
            if rule.match in command:
                matches.append((pid, rule))
                break
    return matches


def current_nice(pid: int) -> int | None:
    """The pid's current niceness, or ``None`` when it cannot be read."""
    try:
        return os.getpriority(os.PRIO_PROCESS, pid)
    except (OSError, AttributeError):
        return None


def set_nice(pid: int, target: int) -> bool:
    """Raise ``pid``'s niceness to ``target``. Returns whether the call succeeded."""
    try:
        os.setpriority(os.PRIO_PROCESS, pid, target)
        return True
    except (OSError, AttributeError):
        return False


def apply_once(
    *,
    target_nice: int = DEFAULT_TARGET_NICE,
    rules: Sequence[Rule] | None = None,
    proc_root: str | pathlib.Path = "/proc",
    scan: Callable[[], list[int]] | None = None,
    nice_of: Callable[[int], int | None] = current_nice,
    setter: Callable[[int, int], bool] = set_nice,
    pid_exclude: int | None = None,
) -> list[Reniced]:
    """Renice every matched process that is still more urgent than its target.

    With ``rules`` the scan looks for the operator's classes and each process gets its rule's
    niceness; without them the built-in turn-worker scan applies ``target_nice``. A process
    already at or below its target is left alone, so repeating the call is free and the loop can
    run every couple of seconds without churning the scheduler. ``pid_exclude`` guards against
    ever touching this process.
    """
    if rules:
        targets: list[tuple[int, int]] = [
            (pid, rule.nice) for pid, rule in find_by_rules(rules, proc_root=proc_root)
        ]
    else:
        scanner = scan or (lambda: find_compute_hosts(proc_root=proc_root))
        targets = [(pid, target_nice) for pid in scanner()]

    applied: list[Reniced] = []
    for pid, target in targets:
        if pid_exclude is not None and pid == pid_exclude:
            continue
        previous = nice_of(pid)
        if previous is None or previous >= target:
            continue
        if not setter(pid, target):
            continue
        applied.append(Reniced(pid=pid, previous=previous, current=target))
    return applied
