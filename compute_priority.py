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
after it starts can, and that is what this module does. Only *raising* nice is used: lowering a
priority of a process we own needs no privilege, and it can never make anything more urgent than
it was.

Everything here is pure and injectable: ``find_compute_hosts`` reads ``/proc``, and ``apply_once``
takes its scan and its setter as arguments, so tests never touch a real process.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

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


@dataclass(frozen=True)
class Reniced:
    """One worker whose niceness was raised by this process."""

    pid: int
    previous: int
    current: int


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


def find_compute_hosts(
    proc_root: str | pathlib.Path = "/proc",
    uid: int | None = None,
    markers: Sequence[str] = COMPUTE_HOST_MARKERS,
) -> list[int]:
    """Pids of turn workers owned by ``uid`` (default: this user).

    Only same-user processes are returned, because that is exactly the set this process is
    allowed to renice. Another user's worker stays untouched — with a minimal setup it is not
    ours to slow down.
    """
    root = pathlib.Path(proc_root)
    if uid is None:
        uid = os.getuid() if hasattr(os, "getuid") else None

    found: list[int] = []
    try:
        # `iterdir()` is a generator: a missing or unreadable root raises while iterating, not
        # when it is called, so the listing has to be materialised inside the guard.
        entries: list[pathlib.Path] = list(root.iterdir())
    except OSError:
        return found

    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if uid is not None:
            try:
                if entry.stat().st_uid != uid:
                    continue
            except OSError:
                continue
        command = _read_cmdline(root, pid)
        if command and any(marker in command for marker in markers):
            found.append(pid)
    return found


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
    scan: Callable[[], list[int]] | None = None,
    nice_of: Callable[[int], int | None] = current_nice,
    setter: Callable[[int, int], bool] = set_nice,
    pid_exclude: int | None = None,
) -> list[Reniced]:
    """Renice every worker that is still more urgent than ``target_nice``.

    A worker already at or below the target is left alone: repeating the call is therefore free,
    and the loop can run every couple of seconds without churning the scheduler. ``pid_exclude``
    guards against ever touching this process.
    """
    if scan is None:
        scan = find_compute_hosts

    applied: list[Reniced] = []
    for pid in scan():
        if pid_exclude is not None and pid == pid_exclude:
            continue
        previous = nice_of(pid)
        if previous is None or previous >= target_nice:
            continue
        if not setter(pid, target_nice):
            continue
        applied.append(Reniced(pid=pid, previous=previous, current=target_nice))
    return applied
