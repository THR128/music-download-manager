# -*- coding: utf-8 -*-
"""计划任务模块（music_sync.schedule）的单元测试。

所有会碰到系统的地方（schtasks / launchctl / 真实用户目录）都 monkeypatch 掉了，
测试不会注册、删除或运行任何真实任务。
"""
from __future__ import annotations

import os
import plistlib

import pytest

from music_sync import schedule


# ---------------------------------------------------------------- 平台 / 命令行

def test_backend_name_matches_platform():
    if os.name == "nt":
        assert schedule.SUPPORTED is True
        assert schedule.backend_name() == "schtasks"
    else:
        assert schedule.backend_name() in ("launchd", "")


@pytest.mark.skipif(os.name != "nt", reason="command_line 的 Windows 分支只在 Windows 上验证")
def test_command_line_windows_puts_config_before_sync(tmp_path):
    config_path = str(tmp_path / "config.json")
    cmd = schedule.command_line(config_path)

    assert len(cmd) == 7
    assert cmd[1:3] == ["-m", "music_sync"]
    assert cmd.index("--config") < cmd.index("sync")        # 踩过的坑
    assert cmd[cmd.index("--config") + 1] == os.path.abspath(config_path)
    assert cmd[cmd.index("sync") + 1:] == ["--gui"]


def test_command_line_mac_uses_python3(monkeypatch):
    monkeypatch.setattr(schedule, "IS_WINDOWS", False)
    monkeypatch.setattr(schedule, "IS_MAC", True)
    cmd = schedule.command_line("/tmp/config.json")
    assert cmd[1:3] == ["-m", "music_sync"]
    assert cmd.index("--config") < cmd.index("sync")
    assert cmd[cmd.index("--config") + 1] == os.path.abspath("/tmp/config.json")


# ---------------------------------------------------------------- 星期换算

@pytest.mark.parametrize("value,expected", [
    ("Sunday", 0), ("sunday", 0), ("周日", 0),   # launchd 里 0 = 周日
    ("Monday", 1), ("monday", 1), ("周一", 1),
    ("Saturday", 6), ("周六", 6),
    (6, 0),                                        # 内部 Monday=0 → 周日=6
    (0, 1),
])
def test_weekday_to_launchd(value, expected):
    assert schedule.weekday_to_launchd(value) == expected


def test_normalize_weekday_rejects_bad_value():
    with pytest.raises(ValueError):
        schedule.normalize_weekday("星期八")


# ---------------------------------------------------------------- plist 生成

def _plist(sched):
    return schedule.build_plist(
        label="com.thr128.music-sync.musicsync",
        args=["/usr/bin/python3", "-m", "music_sync", "--config", "/tmp/config.json",
              "sync", "--gui"],
        cwd="/tmp/project",
        sched=sched,
        log_dir="/tmp/logs",
    )


def test_build_plist_weekly(monkeypatch):
    monkeypatch.setattr(schedule, "IS_MAC", True)
    data = _plist({"mode": "weekly", "weekday": "sunday", "time": "21:00"})

    assert data["StartCalendarInterval"] == {"Weekday": 0, "Hour": 21, "Minute": 0}
    assert "StartInterval" not in data
    assert data["RunAtLoad"] is False
    assert data["WorkingDirectory"] == "/tmp/project"
    assert data["ProgramArguments"][1:3] == ["-m", "music_sync"]
    assert data["EnvironmentVariables"] == {"PYTHONIOENCODING": "utf-8"}
    log_path = os.path.join("/tmp/logs", "com.thr128.music-sync.musicsync.log")
    assert data["StandardOutPath"] == log_path
    assert data["StandardErrorPath"] == data["StandardOutPath"]


def test_build_plist_daily(monkeypatch):
    monkeypatch.setattr(schedule, "IS_MAC", True)
    data = _plist({"mode": "daily", "time": "07:05"})

    assert data["StartCalendarInterval"] == {"Hour": 7, "Minute": 5}
    assert "Weekday" not in data["StartCalendarInterval"]
    assert "StartInterval" not in data


def test_build_plist_hours(monkeypatch):
    monkeypatch.setattr(schedule, "IS_MAC", True)
    data = _plist({"mode": "hours", "hours": 24})

    assert data["StartInterval"] == 24 * 3600
    assert "StartCalendarInterval" not in data


@pytest.mark.parametrize("sched", [
    {"mode": "weekly", "weekday": "monday", "time": "25:00"},
    {"mode": "hours", "hours": 0},
    {"mode": "monthly"},
])
def test_build_plist_rejects_bad_schedule(sched):
    with pytest.raises(ValueError):
        _plist(sched)


def test_label_slug():
    assert schedule._label("Music Sync") == "com.thr128.music-sync.musicsync"
    assert schedule._label("Daily-Job 2") == "com.thr128.music-sync.daily-job2"


# ---------------------------------------------------------------- 不支持平台

def test_install_unsupported_returns_message(monkeypatch):
    monkeypatch.setattr(schedule, "IS_WINDOWS", False)
    monkeypatch.setattr(schedule, "IS_MAC", False)

    ok, message = schedule.install({}, None, weekly=True)
    assert ok is False
    assert "cron" in message

    ok, message = schedule.uninstall()
    assert ok is False and message

    state = schedule.status()
    assert state["installed"] is False
    assert state["detail"]


# ---------------------------------------------------------------- macOS 安装流程

def _fake_launchctl(monkeypatch, calls):
    def fake(args, timeout=20):
        calls.append(list(args))
        return True, ""
    monkeypatch.setattr(schedule, "_launchctl", fake)


def test_status_windows_parses_list_output(monkeypatch):
    class _Proc:
        returncode = 0
        stdout = ("文件夹: \\\n任务名: \\MusicSync\n"
                  "下次运行时间: 2026/11/1 21:00:00\n上次结果: 267011\n")

    monkeypatch.setattr(schedule.subprocess, "run", lambda *a, **k: _Proc())
    state = schedule._status_windows("MusicSync")
    assert state["installed"] is True
    assert state["next_run"] == "2026/11/1 21:00:00"
    assert state["last_result"] == "尚未运行过"
    assert "下次 2026/11/1 21:00:00" in state["detail"]


def test_status_mac_parses_launchctl(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "IS_WINDOWS", False)
    monkeypatch.setattr(schedule, "IS_MAC", True)
    monkeypatch.setattr(schedule, "_home", lambda: str(tmp_path))

    plist_dir = tmp_path / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True)
    (plist_dir / "com.thr128.music-sync.musicsync.plist").write_bytes(b"<plist/>")

    output = ("\tstate = waiting\n\tlast exit code = 0\n"
              "\tnext fire date = Sunday, October 25, 2026 at 9:00:00 PM (1761400800)\n")
    monkeypatch.setattr(schedule, "_launchctl", lambda args, timeout=20: (True, output))

    state = schedule.status("MusicSync")
    assert state["installed"] is True
    assert state["name"] == "MusicSync"
    assert state["next_run"].startswith("Sunday")
    assert state["last_result"] == "0"


def test_status_mac_reports_missing_plist(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "IS_WINDOWS", False)
    monkeypatch.setattr(schedule, "IS_MAC", True)
    monkeypatch.setattr(schedule, "_home", lambda: str(tmp_path))
    monkeypatch.setattr(schedule, "_launchctl",
                        lambda args, timeout=20: pytest.fail("没 plist 不该碰 launchctl"))

    state = schedule.status("MusicSync")
    assert state["installed"] is False
    assert "install_task.sh" in state["detail"]


def test_install_mac_writes_plist(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "IS_WINDOWS", False)
    monkeypatch.setattr(schedule, "IS_MAC", True)
    monkeypatch.setattr(schedule, "_home", lambda: str(tmp_path))
    monkeypatch.setattr(schedule, "command_line",
                        lambda path: ["/usr/bin/python3", "-m", "music_sync",
                                      "--config", str(path), "sync", "--gui"])
    calls: list[list[str]] = []
    _fake_launchctl(monkeypatch, calls)

    config = {"schedule": {"mode": "weekly", "weekday": "sunday", "time": "21:00"}}
    ok, message = schedule.install(config, str(tmp_path / "config.json"),
                                   weekly=True, task_name="Music Sync")
    assert ok is True
    assert "launchd" in message and "21:00" in message

    label = "com.thr128.music-sync.musicsync"
    plist_path = tmp_path / "Library" / "LaunchAgents" / (label + ".plist")
    assert plist_path.is_file()
    with open(plist_path, "rb") as handle:
        data = plistlib.load(handle)
    assert data["Label"] == label
    assert data["StartCalendarInterval"] == {"Weekday": 0, "Hour": 21, "Minute": 0}
    assert ["bootstrap", schedule._gui_domain(), str(plist_path)] in calls


def test_uninstall_mac_removes_plist(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "IS_WINDOWS", False)
    monkeypatch.setattr(schedule, "IS_MAC", True)
    monkeypatch.setattr(schedule, "_home", lambda: str(tmp_path))
    calls: list[list[str]] = []
    _fake_launchctl(monkeypatch, calls)

    plist_dir = tmp_path / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True)
    plist_path = plist_dir / "com.thr128.music-sync.musicsync.plist"
    plist_path.write_bytes(b"<plist/>")

    ok, message = schedule.uninstall(task_name="MusicSync")
    assert ok is True
    assert not plist_path.exists()
    assert any(call[0] == "bootout" for call in calls)
