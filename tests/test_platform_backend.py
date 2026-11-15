"""Platform layer: three backends behind one contract, and never a real process.

Every backend is built with a *fake* ``psutil`` module, so a scan here enumerates dictionaries
and a priority change appends to a list. The only test that touches the real ``psutil`` reads this
process's own command line — it never calls ``nice()`` with an argument. That is deliberate: an
earlier suite in this project reniced real processes on a CI runner by using a fake process scan
with a real setter, and nobody wants that again.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys

import pytest

import compute_priority as core
import platform_backend as pb

_ROOT = pathlib.Path(__file__).resolve().parent.parent

WORKER = ["python3", "-m", "tui_gateway.compute_host"]


# -- a psutil that records instead of doing ---------------------------------------------


class AccessDenied(Exception):
    """Stands in for ``psutil.AccessDenied``."""


class NoSuchProcess(Exception):
    """Stands in for ``psutil.NoSuchProcess``."""


class FakeProcess:
    """One process. ``nice()`` mirrors psutil: no argument reads, one argument writes."""

    def __init__(
        self,
        pid: int,
        cmdline=(),
        *,
        username="elekt",
        nice_value=0,
        cmdline_error=None,
        username_error=None,
        nice_error=None,
        set_error=None,
    ) -> None:
        self.pid = pid
        self._cmdline = list(cmdline)
        self._username = username
        self.nice_value = nice_value
        self.cmdline_error = cmdline_error
        self.username_error = username_error
        self.nice_error = nice_error
        self.set_error = set_error
        self.set_calls: list[int] = []

    def username(self):
        if self.username_error is not None:
            raise self.username_error
        return self._username

    def cmdline(self):
        if self.cmdline_error is not None:
            raise self.cmdline_error
        return list(self._cmdline)

    def nice(self, value=None):
        if value is None:
            if self.nice_error is not None:
                raise self.nice_error
            return self.nice_value
        if self.set_error is not None:
            raise self.set_error
        self.set_calls.append(value)
        self.nice_value = value
        return None


class FakePsutil:
    """The slice of the ``psutil`` API the backends use."""

    IDLE_PRIORITY_CLASS = pb.IDLE_PRIORITY_CLASS
    BELOW_NORMAL_PRIORITY_CLASS = pb.BELOW_NORMAL_PRIORITY_CLASS
    NORMAL_PRIORITY_CLASS = pb.NORMAL_PRIORITY_CLASS
    ABOVE_NORMAL_PRIORITY_CLASS = pb.ABOVE_NORMAL_PRIORITY_CLASS
    HIGH_PRIORITY_CLASS = pb.HIGH_PRIORITY_CLASS
    REALTIME_PRIORITY_CLASS = pb.REALTIME_PRIORITY_CLASS
    AccessDenied = AccessDenied
    NoSuchProcess = NoSuchProcess

    def __init__(self, processes=()) -> None:
        self._by_pid = {proc.pid: proc for proc in processes}

    def process_iter(self):
        return list(self._by_pid.values())

    def Process(self, pid=None):  # noqa: N802 - psutil's own capitalisation
        if pid is None:
            raise RuntimeError("the fake has no 'current process'")
        try:
            return self._by_pid[pid]
        except KeyError:
            raise NoSuchProcess(pid) from None

    def pid_exists(self, pid):
        return pid in self._by_pid


def _windows(processes=(), *, self_username="elekt", psutil_module=None):
    return pb.WindowsBackend(
        psutil_module=FakePsutil(processes) if psutil_module is None else psutil_module,
        platform="win32",
        self_username=self_username,
    )


def _macos(processes=(), *, self_username="elekt", psutil_module=None):
    return pb.MacOsBackend(
        psutil_module=FakePsutil(processes) if psutil_module is None else psutil_module,
        platform="darwin",
        self_username=self_username,
    )


# -- invariant: the nice -> class table is explicit and only ever lowers -----------------


@pytest.mark.parametrize(
    ("nice", "expected"),
    [
        (0, None),
        (1, None),
        (4, None),  # anything below 5 has no class the plugin is allowed to ask for
        (5, pb.BELOW_NORMAL_PRIORITY_CLASS),
        (9, pb.BELOW_NORMAL_PRIORITY_CLASS),
        (10, pb.BELOW_NORMAL_PRIORITY_CLASS),
        (14, pb.BELOW_NORMAL_PRIORITY_CLASS),
        (15, pb.IDLE_PRIORITY_CLASS),
        (19, pb.IDLE_PRIORITY_CLASS),
    ],
)
def test_the_windows_niceness_to_class_table(nice, expected):
    assert pb.nice_to_priority_class(nice) == expected


def test_no_target_ever_maps_to_a_class_above_normal():
    """Raising a priority is the one thing this plugin must never do, on any platform."""
    produced = {pb.nice_to_priority_class(nice) for nice in range(-20, 40)}
    assert produced == {None, pb.BELOW_NORMAL_PRIORITY_CLASS, pb.IDLE_PRIORITY_CLASS}


@pytest.mark.skipif(not sys.platform.startswith("win"),
                    reason="psutil only exposes the priority-class constants on Windows")
def test_the_table_uses_psutil_s_own_constants():
    """The literals are readable without psutil; they must still equal the real thing."""
    psutil = pytest.importorskip("psutil")
    assert pb.IDLE_PRIORITY_CLASS == psutil.IDLE_PRIORITY_CLASS
    assert pb.BELOW_NORMAL_PRIORITY_CLASS == psutil.BELOW_NORMAL_PRIORITY_CLASS
    assert pb.NORMAL_PRIORITY_CLASS == psutil.NORMAL_PRIORITY_CLASS
    assert pb.ABOVE_NORMAL_PRIORITY_CLASS == psutil.ABOVE_NORMAL_PRIORITY_CLASS
    assert pb.HIGH_PRIORITY_CLASS == psutil.HIGH_PRIORITY_CLASS
    assert pb.REALTIME_PRIORITY_CLASS == psutil.REALTIME_PRIORITY_CLASS


@pytest.mark.parametrize(
    ("priority_class", "expected"),
    [
        (pb.IDLE_PRIORITY_CLASS, 19),
        (pb.BELOW_NORMAL_PRIORITY_CLASS, 10),
        (pb.NORMAL_PRIORITY_CLASS, 0),
        (pb.ABOVE_NORMAL_PRIORITY_CLASS, 0),  # reported low on purpose: it must get lowered
        (pb.HIGH_PRIORITY_CLASS, 0),
        (pb.REALTIME_PRIORITY_CLASS, 0),
        (0xDEAD, None),  # a class this build does not know
        (None, None),
    ],
)
def test_a_class_reads_back_on_the_niceness_scale(priority_class, expected):
    assert pb.priority_class_to_nice(priority_class) == expected


# -- invariant: same user only ----------------------------------------------------------


def test_only_the_current_user_s_workers_are_enumerated():
    mine = FakeProcess(101, WORKER)
    theirs = FakeProcess(102, WORKER, username="someone-else")
    assert _windows([mine, theirs]).find_hosts() == [101]


def test_an_unreadable_owner_is_skipped_not_raised():
    opaque = FakeProcess(103, WORKER, username_error=AccessDenied("denied"))
    mine = FakeProcess(101, WORKER)
    assert _windows([opaque, mine]).find_hosts() == [101]


def test_an_unreadable_own_user_name_yields_nothing_rather_than_everything():
    # Fail closed on ownership: not knowing who we are is not a licence to renice anyone.
    assert _windows([FakeProcess(101, WORKER)], self_username=None).find_hosts() == []


def test_windows_user_names_are_compared_case_insensitively():
    proc = FakeProcess(201, WORKER, username="ELEKT")
    assert _windows([proc]).find_hosts() == [201]


def test_elsewhere_user_names_are_compared_exactly():
    proc = FakeProcess(202, WORKER, username="ELEKT")
    assert _macos([proc]).find_hosts() == []


# -- invariant: idempotence --------------------------------------------------------------


def test_a_worker_already_at_its_target_is_not_touched():
    proc = FakeProcess(301, WORKER, nice_value=pb.BELOW_NORMAL_PRIORITY_CLASS)
    applied = _windows([proc]).apply(target_nice=10)
    assert applied == []
    assert proc.set_calls == []


def test_a_worker_already_below_its_target_is_not_raised():
    proc = FakeProcess(302, WORKER, nice_value=pb.IDLE_PRIORITY_CLASS)
    assert _windows([proc]).apply(target_nice=10) == []
    assert proc.set_calls == []


def test_a_normal_worker_is_lowered_once_and_the_next_pass_is_free():
    proc = FakeProcess(303, WORKER, nice_value=pb.NORMAL_PRIORITY_CLASS)
    backend = _windows([proc])

    applied = backend.apply(target_nice=10)
    assert [(item.pid, item.previous, item.current) for item in applied] == [(303, 0, 10)]
    assert proc.set_calls == [pb.BELOW_NORMAL_PRIORITY_CLASS]

    assert backend.apply(target_nice=10) == []
    assert proc.set_calls == [pb.BELOW_NORMAL_PRIORITY_CLASS]


def test_macos_lowers_the_niceness_on_the_ordinary_scale():
    proc = FakeProcess(304, WORKER, nice_value=2)
    applied = _macos([proc]).apply(target_nice=10)
    assert [(item.previous, item.current) for item in applied] == [(2, 10)]
    assert proc.set_calls == [10]


# -- invariant: fail-open ----------------------------------------------------------------


def test_a_process_that_vanishes_mid_scan_is_skipped():
    gone = FakeProcess(401, WORKER, cmdline_error=NoSuchProcess(401))
    alive = FakeProcess(402, WORKER, nice_value=pb.NORMAL_PRIORITY_CLASS)
    applied = _windows([gone, alive]).apply(target_nice=10)
    assert [item.pid for item in applied] == [402]


def test_a_refused_priority_change_is_skipped():
    locked = FakeProcess(403, WORKER, nice_value=pb.NORMAL_PRIORITY_CLASS,
                         set_error=AccessDenied("no"))
    assert _windows([locked]).apply(target_nice=10) == []


def test_an_enumeration_that_raises_yields_nothing():
    class Exploding(FakePsutil):
        def process_iter(self):
            raise AccessDenied("cannot enumerate")

    backend = _windows(psutil_module=Exploding())
    assert backend.find_hosts() == []
    assert backend.apply(target_nice=10) == []


def test_an_unreadable_niceness_skips_that_pid():
    proc = FakeProcess(404, WORKER, nice_error=AccessDenied("no"))
    assert _windows([proc]).apply(target_nice=10) == []


# -- invariant: first matching rule wins -------------------------------------------------


def test_the_first_matching_rule_wins():
    proc = FakeProcess(501, ["node", "mcp-server", "--marker"])
    rules = [
        core.Rule(name="first", match="mcp-server", nice=10),
        core.Rule(name="second", match="marker", nice=15),
    ]
    found = _windows([proc]).find_by_rules(rules)
    assert [(pid, rule.name) for pid, rule in found] == [(501, "first")]


def test_a_rule_s_own_target_is_translated_to_its_class():
    proc = FakeProcess(502, ["node", "mcp-server"], nice_value=pb.NORMAL_PRIORITY_CLASS)
    rules = [core.Rule(name="mcp", match="mcp-server", nice=15)]
    applied = _windows([proc]).apply(rules=rules, target_nice=10)
    assert proc.set_calls == [pb.IDLE_PRIORITY_CLASS]
    assert [item.current for item in applied] == [15]


def test_no_rules_means_only_the_turn_worker_class():
    worker = FakeProcess(503, WORKER, nice_value=pb.NORMAL_PRIORITY_CLASS)
    other = FakeProcess(504, ["node", "mcp-server"], nice_value=pb.NORMAL_PRIORITY_CLASS)
    applied = _windows([worker, other]).apply(target_nice=10)
    assert [item.pid for item in applied] == [503]
    assert other.set_calls == []


# -- invariant: without psutil, nothing happens and the reason is readable ---------------


def test_without_psutil_there_is_no_enumeration_and_no_action():
    backend = pb.WindowsBackend(psutil_module=None, platform="win32")
    assert backend.available is False
    assert backend.find_hosts() == []
    assert backend.apply(target_nice=10) == []


def test_without_psutil_the_reason_names_psutil():
    backend = pb.WindowsBackend(psutil_module=None, platform="win32")
    status = backend.status()
    assert status["available"] is False
    assert "psutil" in str(status["reason"])


def test_active_backend_reports_a_missing_psutil_instead_of_pretending(monkeypatch):
    monkeypatch.setattr(pb, "load_psutil", lambda: None)
    monkeypatch.setattr(pb.sys, "platform", "win32")
    backend = pb.active()
    assert backend.available is False
    assert "psutil" in (backend.reason or "")
    assert backend.apply() == []


def test_a_windows_target_below_five_is_reported_as_impossible():
    backend = _windows()
    assert backend.target_problem(10) is None
    assert "5" in (backend.target_problem(2) or "")
    proc = FakeProcess(601, WORKER, nice_value=pb.NORMAL_PRIORITY_CLASS)
    assert _windows([proc]).apply(target_nice=2) == []
    assert proc.set_calls == []


# -- the status route --------------------------------------------------------------------


def _load_plugin_api(monkeypatch):
    """``plugin_api`` imported as Hermes imports it, with the loop switched off."""
    monkeypatch.setenv("HERMES_COMPUTE_PRIORITY", "0")
    spec = importlib.util.spec_from_file_location(
        "hermes_compute_priority_api_platform_under_test",
        _ROOT / "dashboard" / "plugin_api.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_status_route_says_why_nothing_runs_when_psutil_is_missing(monkeypatch):
    module = _load_plugin_api(monkeypatch)
    monkeypatch.setattr(module, "_NANNY", None)
    monkeypatch.setattr(module, "_BACKEND",
                        pb.WindowsBackend(psutil_module=None, platform="win32"))
    monkeypatch.setattr(module, "_DISABLED_REASON", None)

    payload = module.status_payload()
    assert payload["enabled"] is False
    assert payload["available"] is False
    assert "psutil" in str(payload["reason"])


def test_the_status_route_names_the_switch_when_it_is_switched_off(monkeypatch):
    module = _load_plugin_api(monkeypatch)
    monkeypatch.setattr(module, "_NANNY", None)
    monkeypatch.setattr(module, "_DISABLED_REASON", "disabled by HERMES_COMPUTE_PRIORITY")
    payload = module.status_payload()
    assert payload["enabled"] is False
    assert payload["reason"] == "disabled by HERMES_COMPUTE_PRIORITY"


def test_the_status_route_reports_the_backend_it_runs(monkeypatch):
    module = _load_plugin_api(monkeypatch)
    nanny = module.WorkerNanny(target_nice=10, interval_s=60.0, backend=_windows())
    monkeypatch.setattr(module, "_NANNY", nanny)

    payload = module.status_payload()
    assert payload["enabled"] is True
    assert payload["backend"] == "psutil-win32"
    assert payload["platform"] == "win32"
    assert "reason" not in payload


def test_the_scan_loop_uses_the_injected_backend(monkeypatch):
    """The loop must not reach for ``/proc`` on a platform that has none."""
    module = _load_plugin_api(monkeypatch)
    proc = FakeProcess(701, WORKER, nice_value=pb.NORMAL_PRIORITY_CLASS)
    nanny = module.WorkerNanny(target_nice=10, interval_s=60.0, backend=_windows([proc]))
    monkeypatch.setattr(module, "_NANNY", nanny)

    applied = nanny.tick()
    assert applied == [{"pid": 701, "from": 0, "to": 10}]
    assert proc.set_calls == [pb.BELOW_NORMAL_PRIORITY_CLASS]


# -- the backends themselves -------------------------------------------------------------


def test_each_platform_gets_its_own_backend():
    backend = pb.active()
    if sys.platform.startswith("linux"):
        assert isinstance(backend, pb.ProcfsBackend)
    elif sys.platform == "darwin":
        assert isinstance(backend, pb.MacOsBackend)
    elif sys.platform.startswith("win"):
        assert isinstance(backend, pb.WindowsBackend)
    else:
        assert isinstance(backend, pb.UnsupportedBackend)
        assert backend.available is False


@pytest.mark.skipif(not hasattr(os, "getpriority"), reason="the procfs backend is POSIX-only")
def test_the_procfs_backend_leaves_the_linux_path_untouched(tmp_path):
    directory = tmp_path / "4242"
    directory.mkdir()
    (directory / "cmdline").write_bytes(b"python3\x00-m\x00tui_gateway.compute_host")
    backend = pb.ProcfsBackend(proc_root=str(tmp_path))
    assert backend.available is True
    assert backend.find_hosts() == [4242]


@pytest.mark.skipif(sys.platform not in {"darwin", "win32"},
                    reason="the psutil backends are for macOS and Windows")
def test_psutil_can_read_our_own_command_line():
    """The one test that uses the real psutil, and it only *reads* — never ``nice(value)``."""
    pytest.importorskip("psutil")
    backend = pb.active()
    assert backend.available is True
    command = backend.cmdline_of(os.getpid())
    assert command, "psutil could not read this process's own command line"
    assert "python" in command.lower()
