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

# -- opt-in rules: off unless the operator asks for them -----------------------


def test_no_rules_by_default():
    assert core.parse_rules(None) == []
    assert core.parse_rules("") == []
    assert core.parse_rules("   ") == []


def test_an_invalid_rule_list_is_ignored_not_raised():
    assert core.parse_rules("not json") == []
    assert core.parse_rules('{"name": "x"}') == []          # not a list
    assert core.parse_rules('[1, 2, "a"]') == []


def test_a_valid_rule_is_parsed():
    rules = core.parse_rules('[{"name": "mcp", "match": "mcp-server", "nice": 15}]')
    assert rules == [core.Rule(name="mcp", match="mcp-server", nice=15)]


def test_rules_missing_a_name_or_a_match_are_dropped():
    rules = core.parse_rules(
        '[{"name": "", "match": "x", "nice": 5}, {"name": "y", "match": "", "nice": 5},'
        ' {"name": "ok", "match": "zzz", "nice": 5}]'
    )
    assert [r.name for r in rules] == ["ok"]


def test_a_rule_never_asks_for_a_higher_priority():
    # Negative niceness would need a privilege this plugin does not want, and 20+ is out of range.
    assert core.parse_rules('[{"name": "n", "match": "m", "nice": -5}]') == []
    assert core.parse_rules('[{"name": "n", "match": "m", "nice": 20}]') == []


def test_find_by_rules_matches_only_the_configured_class(tmp_path):
    _fake_proc(tmp_path, 10, "python3 -m tui_gateway.compute_host")
    _fake_proc(tmp_path, 11, "node mcp-server --stdio")
    rules = [core.Rule(name="mcp", match="mcp-server", nice=15)]
    assert [pid for pid, _ in core.find_by_rules(rules, proc_root=tmp_path, uid=None)] == [11]


def test_the_first_matching_rule_wins(tmp_path):
    _fake_proc(tmp_path, 12, "node mcp-server --marker")
    rules = [
        core.Rule(name="first", match="mcp-server", nice=5),
        core.Rule(name="second", match="marker", nice=15),
    ]
    assert [(pid, rule.name) for pid, rule in core.find_by_rules(rules, proc_root=tmp_path, uid=None)] == [(12, "first")]


def test_rules_apply_their_own_niceness(tmp_path):
    _fake_proc(tmp_path, 20, "node mcp-server")
    _fake_proc(tmp_path, 21, "python3 -m hermes_cli.plugin_host_child")
    calls: list[tuple[int, int]] = []
    rules = [
        core.Rule(name="mcp", match="mcp-server", nice=15),
        core.Rule(name="plugin-host", match="plugin_host_child", nice=8),
    ]
    applied = core.apply_once(
        rules=rules,
        proc_root=tmp_path,
        nice_of=lambda pid: 0,
        setter=lambda pid, nice: calls.append((pid, nice)) or True,
    )
    assert sorted(calls) == [(20, 15), (21, 8)]
    assert {a.current for a in applied} == {8, 15}


def test_a_too_short_match_is_refused():
    # "a" matches most command lines on a machine: refusing short matches is what keeps a typo
    # from renicing half the box. Found the hard way — a test using it reniced real processes.
    assert core.parse_rules('[{"name": "x", "match": "a", "nice": 5}]') == []
    assert core.parse_rules('[{"name": "x", "match": "ab", "nice": 5}]') == []
    assert core.parse_rules('[{"name": "x", "match": "abc", "nice": 5}]') != []
