"""Tests for the pure core: no real process is ever touched.

``apply_once`` takes its scan and its setter as arguments on purpose: a test that called
``os.setpriority`` would renice processes belonging to whoever runs the suite.
"""

from __future__ import annotations

import pathlib

import pytest

import compute_priority as core


def _fake_proc(root: pathlib.Path, pid: int, cmdline: str, uid: int | None = None) -> None:
    directory = root / str(pid)
    directory.mkdir(parents=True)
    (directory / "cmdline").write_bytes(cmdline.replace(" ", "\x00").encode())


def test_finds_a_worker_by_its_module_name(tmp_path):
    _fake_proc(tmp_path, 4242, "python3 -m tui_gateway.compute_host")
    assert core.find_compute_hosts(proc_root=tmp_path, uid=None) == [4242]


def test_ignores_everything_else(tmp_path):
    _fake_proc(tmp_path, 1, "python3 -m hermes_cli.main dashboard")
    _fake_proc(tmp_path, 2, "bash -c echo not-a-worker")
    (tmp_path / "3").mkdir()
    assert core.find_compute_hosts(proc_root=tmp_path, uid=None) == []


def test_an_unreadable_cmdline_is_not_an_error(tmp_path):
    (tmp_path / "7").mkdir()
    _fake_proc(tmp_path, 8, "python3 -m tui_gateway.compute_host")
    assert core.find_compute_hosts(proc_root=tmp_path, uid=None) == [8]


def test_a_missing_proc_root_yields_nothing(tmp_path):
    assert core.find_compute_hosts(proc_root=tmp_path / "absent", uid=None) == []


def test_only_raises_priority_never_lowers_it():
    calls: list[tuple[int, int]] = []

    applied = core.apply_once(
        target_nice=10,
        scan=lambda: [100],
        nice_of=lambda pid: 0,
        setter=lambda pid, nice: calls.append((pid, nice)) or True,
    )

    assert calls == [(100, 10)]
    assert applied == [core.Reniced(pid=100, previous=0, current=10)]


def test_a_worker_already_reniced_is_left_alone():
    calls: list[tuple[int, int]] = []

    applied = core.apply_once(
        target_nice=10,
        scan=lambda: [100],
        nice_of=lambda pid: 10,
        setter=lambda pid, nice: calls.append((pid, nice)) or True,
    )

    assert calls == []
    assert applied == []


def test_a_worker_more_urgent_than_the_target_is_never_lowered_to_it():
    # -5 is below 10: the job is to make it *less* urgent, so it is reniced up, not skipped.
    calls: list[tuple[int, int]] = []
    applied = core.apply_once(
        target_nice=10,
        scan=lambda: [100],
        nice_of=lambda pid: -5,
        setter=lambda pid, nice: calls.append((pid, nice)) or True,
    )
    assert calls == [(100, 10)]
    assert applied[0].previous == -5


def test_an_unreadable_niceness_skips_that_pid():
    calls: list[tuple[int, int]] = []
    applied = core.apply_once(
        target_nice=10,
        scan=lambda: [100],
        nice_of=lambda pid: None,
        setter=lambda pid, nice: calls.append((pid, nice)) or True,
    )
    assert calls == []
    assert applied == []


def test_a_refused_setpriority_skips_that_pid():
    applied = core.apply_once(
        target_nice=10,
        scan=lambda: [100],
        nice_of=lambda pid: 0,
        setter=lambda pid, nice: False,
    )
    assert applied == []


def test_this_process_is_never_touched():
    calls: list[tuple[int, int]] = []
    core.apply_once(
        target_nice=10,
        scan=lambda: [100, 200],
        nice_of=lambda pid: 0,
        setter=lambda pid, nice: calls.append((pid, nice)) or True,
        pid_exclude=200,
    )
    assert calls == [(100, 10)]


def test_several_workers_are_all_reniced():
    calls: list[tuple[int, int]] = []
    applied = core.apply_once(
        target_nice=10,
        scan=lambda: [11, 12, 13],
        nice_of=lambda pid: 0,
        setter=lambda pid, nice: calls.append((pid, nice)) or True,
    )
    assert len(applied) == 3
    assert calls == [(11, 10), (12, 10), (13, 10)]
