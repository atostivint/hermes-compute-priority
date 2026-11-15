"""Platform layer behind ``compute_priority``'s contract: two scales, three backends, one behaviour.

Why a separate module
---------------------
``compute_priority`` answers two questions — *which processes are workers?* and *how urgent is
this process / make it less urgent*. On Linux both answers come from the kernel directly: one
``/proc`` listing and ``os.setpriority``. macOS and Windows have no ``/proc``, and Windows has no
0–19 niceness scale at all: ``psutil.Process.nice()`` there reads and writes a **priority class**
(``IDLE_PRIORITY_CLASS``, ``BELOW_NORMAL_PRIORITY_CLASS``, …). Keeping that translation here, and
out of the core, is what lets the Linux path stay exactly as it was — the procfs reader below is a
thin call onto the existing functions, not a rewrite of them.

``psutil`` is already a core hermes-agent dependency, so using it adds nothing to install. It is
still optional *for this plugin*: without it, macOS and Windows enumerate nothing, do nothing, and
``/status`` says so. Nothing here ever raises — a process that vanishes mid-scan, a command line
that cannot be read, a refused ``SetPriorityClass`` all mean "skip this pid".

The niceness → class table is explicit and documented (README "Platforms"), because it is a policy
choice, not arithmetic:

===========================  =======================  ==============================
target niceness (0–19)       Windows priority class   effect
===========================  =======================  ==============================
``>= 15``                    ``IDLE_PRIORITY_CLASS``  the lowest class Windows offers
``>= 5`` and ``< 15``        ``BELOW_NORMAL_…``       behind the serving process
``< 5``                      *(no action)*            normal is already the floor the
                                                      plugin is allowed to ask for
===========================  =======================  ==============================

Below 5 nothing happens on purpose: the only classes left are NORMAL (the ordinary priority, i.e.
no change at all) and classes *above* NORMAL, which would make a process more urgent than the
default. This plugin only ever lowers.

Every backend takes its ``psutil`` module as an argument, so the tests drive a fake one and never
enumerate, let alone renice, a real process.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Iterator, Sequence

import compute_priority as core

# -- Windows priority classes, from winbase.h -----------------------------------------------
# psutil exposes the same values as module constants; the literals are kept here so that the
# table and its tests read without psutil installed (the CI matrix has a lane without it).
IDLE_PRIORITY_CLASS = 0x00000040
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
NORMAL_PRIORITY_CLASS = 0x00000020
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
HIGH_PRIORITY_CLASS = 0x00000080
REALTIME_PRIORITY_CLASS = 0x00000100

#: The nice → class table, ordered from the lowest class up: the first threshold ``nice`` reaches
#: or exceeds wins. Only two rows exist, and the absence of a row for ``< 5`` is the documented
#: "no action" case — see ``nice_to_priority_class``.
WINDOWS_PRIORITY_CLASS_BY_NICE: tuple[tuple[int, int], ...] = (
    (15, IDLE_PRIORITY_CLASS),
    (5, BELOW_NORMAL_PRIORITY_CLASS),
)

#: The value a class is reported *as*, so the core's idempotence test (``previous >= target``)
#: works on Windows. ``IDLE`` is mapped to 19 and ``BELOW_NORMAL`` to 10: the niceness the plugin
#: would have asked for to land there. Anything at or above NORMAL reports 0 — coarse on purpose,
#: it only has to be *below* every target the plugin uses so the process is lowered. A class this
#: table does not know reports ``None`` and the pid is skipped.
WINDOWS_NICE_BY_PRIORITY_CLASS: dict[int, int] = {
    IDLE_PRIORITY_CLASS: 19,
    BELOW_NORMAL_PRIORITY_CLASS: 10,
    NORMAL_PRIORITY_CLASS: 0,
    ABOVE_NORMAL_PRIORITY_CLASS: 0,
    HIGH_PRIORITY_CLASS: 0,
    REALTIME_PRIORITY_CLASS: 0,
}

_MISSING_PSUTIL = (
    "psutil is not installed, so processes cannot be enumerated on this platform; "
    "install it with `pip install psutil` (hermes-agent already depends on it)"
)

_UNSET = object()


def nice_to_priority_class(nice: int) -> int | None:
    """The Windows priority class for a target niceness, or ``None`` when nothing must be done.

    ``None`` is returned for anything below 5 on purpose: NORMAL is the ordinary priority (no
    change), and every other remaining class is *above* NORMAL, which would raise a process's
    priority rather than lower it.
    """
    # Declaration order is highest threshold first, so the first row reached is the right one.
    for threshold, priority_class in WINDOWS_PRIORITY_CLASS_BY_NICE:
        if nice >= threshold:
            return priority_class
    return None


def priority_class_to_nice(priority_class: int | None) -> int | None:
    """A Windows priority class read back on the 0–19 scale, or ``None`` when it is unknown."""
    if priority_class is None:
        return None
    return WINDOWS_NICE_BY_PRIORITY_CLASS.get(priority_class)


def load_psutil() -> Any | None:
    """``psutil`` when importable, ``None`` otherwise. A broken install counts as absent."""
    try:
        import psutil
    except Exception:
        return None
    return psutil


# -- the backends -----------------------------------------------------------------------------


class Backend:
    """One platform's answer to the two questions, plus why it cannot answer them."""

    name = "abstract"

    def __init__(self, platform: str | None = None) -> None:
        self.platform = platform or sys.platform
        self.available = False
        self.reason: str | None = "no backend selected"

    def status(self) -> dict[str, object]:
        """What ``/status`` reports about this backend. Never raises."""
        return {
            "backend": self.name,
            "platform": self.platform,
            "available": self.available,
            "reason": self.reason,
        }

    def target_problem(self, target_nice: int) -> str | None:
        """Why ``target_nice`` cannot be honoured here, or ``None`` when it can."""
        return None

    def find_hosts(self, markers: Sequence[str] = core.COMPUTE_HOST_MARKERS) -> list[int]:
        raise NotImplementedError

    def find_by_rules(self, rules: Sequence[core.Rule]) -> list[tuple[int, core.Rule]]:
        raise NotImplementedError

    def nice_of(self, pid: int) -> int | None:
        raise NotImplementedError

    def set_nice(self, pid: int, target: int) -> bool:
        raise NotImplementedError

    def apply(
        self,
        *,
        target_nice: int = core.DEFAULT_TARGET_NICE,
        rules: Sequence[core.Rule] | None = None,
        pid_exclude: int | None = None,
    ) -> list[core.Reniced]:
        """Run the core algorithm against this platform's enumeration and setter.

        The algorithm itself — idempotence, first-rule-wins, fail-open — stays in
        ``compute_priority.apply_once``; this only supplies the two callables and, for rules, the
        finder. An unavailable backend returns ``[]`` without touching anything.
        """
        if not self.available:
            return []
        finder = (lambda declared: self.find_by_rules(declared)) if rules else None
        return core.apply_once(
            target_nice=target_nice,
            rules=rules,
            scan=lambda: self.find_hosts(core.COMPUTE_HOST_MARKERS),
            nice_of=self.nice_of,
            setter=self.set_nice,
            pid_exclude=pid_exclude,
            finder=finder,
        )


class ProcfsBackend(Backend):
    """Linux: the path that shipped in 0.2.x, unchanged and with no new dependency.

    Every method is one call onto the functions ``compute_priority`` already exports, so the
    ``/proc`` reader, the ``st_uid`` filter and ``os.setpriority`` keep exactly one implementation.
    """

    name = "procfs"

    def __init__(self, proc_root: str = "/proc", platform: str | None = None) -> None:
        super().__init__(platform)
        self.proc_root = proc_root
        self.available = callable(getattr(os, "getpriority", None)) and callable(
            getattr(os, "setpriority", None)
        )
        self.reason = (
            None
            if self.available
            else "this Python build exposes no os.getpriority/os.setpriority"
        )

    def find_hosts(self, markers: Sequence[str] = core.COMPUTE_HOST_MARKERS) -> list[int]:
        return core.find_compute_hosts(proc_root=self.proc_root, markers=markers)

    def find_by_rules(self, rules: Sequence[core.Rule]) -> list[tuple[int, core.Rule]]:
        return core.find_by_rules(rules, proc_root=self.proc_root)

    def nice_of(self, pid: int) -> int | None:
        return core.current_nice(pid)

    def set_nice(self, pid: int, target: int) -> bool:
        return core.set_nice(pid, target)


class PsutilBackend(Backend):
    """macOS and Windows enumeration: ``psutil.Process.cmdline()``, filtered to this user.

    ``uids()`` is POSIX-only, so ownership is compared by user name instead. Every read is
    guarded: a process that exits between the listing and the read, or one whose command line
    the OS refuses to show, is skipped — never an exception out of a scan that runs every two
    seconds inside somebody's dashboard.
    """

    name = "psutil"

    def __init__(
        self,
        psutil_module: Any | object = _UNSET,
        platform: str | None = None,
        self_username: str | None | object = _UNSET,
    ) -> None:
        super().__init__(platform)
        # `_UNSET` means "import it"; an explicit `None` means "this platform has none", which is
        # how a test models a psutil-less machine without uninstalling anything.
        if psutil_module is _UNSET:
            psutil_module = load_psutil()
        self.psutil = psutil_module
        self.available = self.psutil is not None
        self.reason = None if self.available else _MISSING_PSUTIL
        self._self_username = self_username

    # -- ownership -------------------------------------------------------------------------
    def my_username(self) -> str | None:
        """This process's user name, or ``None`` when it cannot be read."""
        if self._self_username is not _UNSET:
            return self._self_username  # type: ignore[return-value]
        try:
            return self.psutil.Process().username()
        except Exception:
            return None

    def _same_user(self, handle: Any) -> bool:
        mine = self.my_username()
        if not mine:
            return False
        try:
            theirs = handle.username()
        except Exception:
            # An unreadable owner is not evidence that the process is ours.
            return False
        if not theirs:
            return False
        return _same_name(str(theirs), str(mine), self.platform)

    # -- enumeration -----------------------------------------------------------------------
    def _candidates(self) -> Iterator[Any]:
        try:
            handles = list(self.psutil.process_iter())
        except Exception:
            return
        for handle in handles:
            try:
                if not self._same_user(handle):
                    continue
            except Exception:
                continue
            yield handle

    @staticmethod
    def _command_of(handle: Any) -> str:
        try:
            return " ".join(handle.cmdline())
        except Exception:
            return ""

    def cmdline_of(self, pid: int) -> str:
        """The pid's command line, or ``""``. Read-only: used by the psutil smoke test."""
        try:
            return self._command_of(self.psutil.Process(pid))
        except Exception:
            return ""

    def find_hosts(self, markers: Sequence[str] = core.COMPUTE_HOST_MARKERS) -> list[int]:
        if not self.available:
            return []
        found: list[int] = []
        for handle in self._candidates():
            command = self._command_of(handle)
            if command and any(marker in command for marker in markers):
                found.append(handle.pid)
        return found

    def find_by_rules(self, rules: Sequence[core.Rule]) -> list[tuple[int, core.Rule]]:
        matches: list[tuple[int, core.Rule]] = []
        if not self.available or not rules:
            return matches
        for handle in self._candidates():
            command = self._command_of(handle)
            if not command:
                continue
            for rule in rules:
                if rule.match in command:
                    matches.append((handle.pid, rule))
                    break
        return matches

    # -- priority --------------------------------------------------------------------------
    def _process(self, pid: int) -> Any | None:
        try:
            return self.psutil.Process(pid)
        except Exception:
            return None

    def nice_of(self, pid: int) -> int | None:  # pragma: no cover - overridden
        raise NotImplementedError

    def set_nice(self, pid: int, target: int) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError


class MacOsBackend(PsutilBackend):
    """macOS: psutil for both halves. ``Process.nice(10)`` is an ordinary POSIX niceness."""

    name = "psutil-posix"

    def nice_of(self, pid: int) -> int | None:
        handle = self._process(pid)
        if handle is None:
            return None
        try:
            return int(handle.nice())
        except Exception:
            return None

    def set_nice(self, pid: int, target: int) -> bool:
        if not 0 <= target <= core.MAX_RULE_NICE:
            return False
        handle = self._process(pid)
        if handle is None:
            return False
        try:
            handle.nice(target)
            return True
        except Exception:
            return False

    def target_problem(self, target_nice: int) -> str | None:
        if not 0 <= target_nice <= core.MAX_RULE_NICE:
            return f"target niceness {target_nice} is outside 0..{core.MAX_RULE_NICE}"
        return None


class WindowsBackend(PsutilBackend):
    """Windows: psutil enumeration, priority *classes* instead of a 0–19 scale.

    ``nice()`` reads a class there, so both directions go through the explicit table at the top
    of this module. A target below 5 maps to no class: the plugin has nothing it is allowed to
    do, and it says so through ``target_problem`` rather than pretending to work.
    """

    name = "psutil-win32"

    def nice_of(self, pid: int) -> int | None:
        handle = self._process(pid)
        if handle is None:
            return None
        try:
            raw = handle.nice()
        except Exception:
            return None
        return priority_class_to_nice(raw)

    def set_nice(self, pid: int, target: int) -> bool:
        priority_class = nice_to_priority_class(target)
        if priority_class is None:
            return False
        try:
            if not self.psutil.pid_exists(pid):
                return False
        except Exception:
            return False
        handle = self._process(pid)
        if handle is None:
            return False
        try:
            handle.nice(priority_class)
            return True
        except Exception:
            return False

    def target_problem(self, target_nice: int) -> str | None:
        if nice_to_priority_class(target_nice) is not None:
            return None
        return (
            f"target niceness {target_nice} maps to no Windows priority class below NORMAL, "
            "so no worker will be changed; use 5 or more (5-14 = BELOW_NORMAL, 15-19 = IDLE)"
        )


class UnsupportedBackend(Backend):
    """Anything else (FreeBSD, a bare interpreter): loads, does nothing, says why."""

    name = "unsupported"

    def __init__(self, platform: str | None = None) -> None:
        super().__init__(platform)
        self.reason = f"no backend for platform {self.platform!r}"


def _same_name(theirs: str, mine: str, platform: str) -> bool:
    """User-name comparison. Case-insensitive on Windows, exactly as stored elsewhere.

    Windows account names are case-insensitive and psutil returns them as the OS stored them
    (``DOMAIN\\User``), so a case-sensitive compare would silently filter out every candidate.
    """
    if platform.startswith("win"):
        return theirs.casefold() == mine.casefold()
    return theirs == mine


def active() -> Backend:
    """The backend for the platform this process runs on.

    Deliberately not cached: it is called once at import, and a fresh object keeps every test
    independent.
    """
    platform = sys.platform
    if platform.startswith("linux"):
        return ProcfsBackend(platform=platform)
    if platform == "darwin":
        return MacOsBackend(platform=platform)
    if platform.startswith("win") or platform in {"cygwin", "msys"}:
        return WindowsBackend(platform=platform)
    return UnsupportedBackend(platform=platform)
