# -*- coding: utf-8 -*-
"""跨平台计划任务管理：Windows 用 schtasks，macOS 用 launchd。

对外只暴露这几个函数（``cli.py`` 与 ``selfcheck.py`` 都只调用它们）::

    SUPPORTED           当前系统能不能自动注册任务
    backend_name()      "schtasks" / "launchd" / ""
    command_line()      任务真正要执行的命令行
    install()           注册 / 更新任务
    uninstall()         删除任务
    run_now()           立刻跑一次
    status()            查任务状态

Windows 的安装细节仍然全部留在 ``scripts/install_task.ps1``（先删后建、任务 XML、
StartWhenAvailable、工作目录都在那里），本模块只负责拼参数并调用它，保证老用户
的行为和输出一个字都不变。

macOS 用 LaunchAgent（``~/Library/LaunchAgents/com.thr128.music-sync.<名字>.plist``），
plist 一律用 :mod:`plistlib` 生成，不手拼 XML。

星期约定（重要）
----------------
* 本模块内部统一用 **Monday=0 … Sunday=6**（与 ``datetime.date.weekday()`` 一致）；
* Windows 的 schtasks 认 Monday…Sunday 英文名，原样交给 PowerShell 脚本；
* macOS 的 launchd ``Weekday`` 是 **0=Sunday … 6=Saturday**，
  所以换算公式是 ``(monday_zero + 1) % 7``。
换算统一走 :func:`weekday_to_launchd`，函数 docstring 里写清了输入输出。
"""
from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys

from . import config as cfgmod
from .util import get_logger

# 平台开关：放在模块级，测试里可以 monkeypatch 成别的东西（不会真的去装任务）
IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
SUPPORTED = IS_WINDOWS or IS_MAC

#: Linux 等平台上的统一提示（面向用户，不提技术黑话）
UNSUPPORTED_HINT = "当前系统暂不支持自动注册计划任务；Linux 可自行用 cron 调 `python -m music_sync sync`"

LABEL_PREFIX = "com.thr128.music-sync"
_DEFAULT_TASK_NAME = "MusicSync"
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")

_WEEKDAY_NAMES = {
    "monday": 0, "mon": 0, "周一": 0, "星期一": 0,
    "tuesday": 1, "tue": 1, "周二": 1, "星期二": 1,
    "wednesday": 2, "wed": 2, "周三": 2, "星期三": 2,
    "thursday": 3, "thu": 3, "周四": 3, "星期四": 3,
    "friday": 4, "fri": 4, "周五": 4, "星期五": 4,
    "saturday": 5, "sat": 5, "周六": 5, "星期六": 5,
    "sunday": 6, "sun": 6, "周日": 6, "星期日": 6, "星期天": 6,
}
_WEEKDAY_CN = "一二三四五六日"


# ---------------------------------------------------------------- 基础

def _backend() -> str:
    if IS_WINDOWS:
        return "schtasks"
    if IS_MAC:
        return "launchd"
    return ""


def backend_name() -> str:
    """返回当前平台使用的后端名；不支持时返回空串。"""
    return _backend()


def _project_root() -> str:
    """从 ``music_sync/__file__`` 往上两级推算项目根目录。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _task_name_from(config: dict | None) -> str:
    name = ((config or {}).get("schedule") or {}).get("task_name")
    return str(name or _DEFAULT_TASK_NAME)


def command_line(config_path: str | None) -> list[str]:
    """任务要执行的完整命令（已拆分好的参数列表）。

    注意 ``--config`` 是全局参数，必须放在子命令 ``sync`` 前面，否则 argparse 会报
    ``unrecognized arguments: --config``（这是已经踩过的坑）。
    """
    path = cfgmod.config_path(config_path)
    if IS_WINDOWS:
        exe = sys.executable or ""
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe") if exe else ""
        if pyw and os.path.isfile(pyw):
            exe = pyw                      # 无控制台窗口，定时任务不闪黑窗
    else:
        exe = sys.executable or ""
        if not os.path.isabs(exe):
            exe = shutil.which("python3") or exe or "python3"
    return [exe, "-m", "music_sync", "--config", path, "sync", "--gui"]


# ---------------------------------------------------------------- 星期 / 时间

def normalize_weekday(value) -> int:
    """把各种写法统一成 **Monday=0 … Sunday=6**。

    接受英文全称 / 三字母缩写 / 中文「周一…周日」/ 整数 0-6（按周一=0 解释）。
    """
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        raise ValueError(f"星期要在 0~6 之间（周一=0），现在是：{value}")
    text = str(value or "").strip().lower()
    if text.isdigit():
        return normalize_weekday(int(text))
    if text in _WEEKDAY_NAMES:
        return _WEEKDAY_NAMES[text]
    raise ValueError(f"星期写错了：{value}（可选 Monday~Sunday 或 周一~周日）")


def weekday_to_launchd(value) -> int:
    """星期换算：内部 Monday=0…Sunday=6 → launchd Weekday 0=Sunday…6=Saturday。

    ``weekday_to_launchd("Sunday") == 0``、``weekday_to_launchd("Monday") == 1``。
    """
    return (normalize_weekday(value) + 1) % 7


def _parse_time(text) -> tuple[int, int]:
    value = str(text or "").strip()
    if not _TIME_RE.match(value):
        raise ValueError(f"时间格式不对：{text}（要写成 HH:MM，例如 21:00）")
    hour, _, minute = value.partition(":")
    return int(hour), int(minute)


# ---------------------------------------------------------------- macOS 纯函数

def _label(task_name: str) -> str:
    """launchd 的 Label：``com.thr128.music-sync.<任务名去空格小写>``。"""
    slug = re.sub(r"\s+", "", str(task_name or "").strip().lower())
    slug = re.sub(r"[^a-z0-9._-]", "", slug) or "musicsync"
    return f"{LABEL_PREFIX}.{slug}"


def _home() -> str:
    return os.path.expanduser("~")


def _launch_agents_dir() -> str:
    return os.path.join(_home(), "Library", "LaunchAgents")


def _log_dir() -> str:
    return os.path.join(_home(), "Library", "Logs", "music-sync")


def _plist_path(task_name: str) -> str:
    return os.path.join(_launch_agents_dir(), _label(task_name) + ".plist")


def build_plist(label: str, args: list[str], cwd: str, sched: dict, log_dir: str) -> dict:
    """根据调度描述生成 LaunchAgent plist 内容（纯函数，不碰文件系统）。

    :param sched: 形如 ``{"mode": "weekly", "weekday": "sunday", "time": "21:00"}`` /
        ``{"mode": "daily", "time": "07:30"}`` / ``{"mode": "hours", "hours": 24}``。
    """
    mode = str((sched or {}).get("mode") or "weekly").strip().lower()
    plist: dict = {
        "Label": label,
        "ProgramArguments": [str(arg) for arg in args],
        "WorkingDirectory": cwd,
        "RunAtLoad": False,
        "StandardOutPath": os.path.join(log_dir, label + ".log"),
        "StandardErrorPath": os.path.join(log_dir, label + ".log"),
        "EnvironmentVariables": {"PYTHONIOENCODING": "utf-8"},
    }
    if mode == "weekly":
        hour, minute = _parse_time((sched or {}).get("time") or "21:00")
        plist["StartCalendarInterval"] = {
            "Weekday": weekday_to_launchd((sched or {}).get("weekday") or "sunday"),
            "Hour": hour,
            "Minute": minute,
        }
    elif mode == "daily":
        hour, minute = _parse_time((sched or {}).get("time") or "21:00")
        plist["StartCalendarInterval"] = {"Hour": hour, "Minute": minute}
    elif mode == "hours":
        raw = (sched or {}).get("hours")
        try:
            interval = int(168 if raw is None else raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"间隔小时数不是整数：{raw}") from exc
        if not 1 <= interval <= 8760:
            raise ValueError(f"间隔小时数要在 1~8760 之间，现在是：{interval}")
        plist["StartInterval"] = interval * 3600
    else:
        raise ValueError(f"不认识的触发方式：{mode}")
    return plist


def _resolve_schedule(config: dict | None, *, weekly: bool, daily: bool, hours,
                      weekday, time) -> dict:
    """命令行参数优先、缺省读配置，最后落到 weekly/21:00/168 小时。"""
    sched_cfg = (config or {}).get("schedule") or {}
    mode = ""
    if hours:
        mode = "hours"
    elif daily:
        mode = "daily"
    elif weekly:
        mode = "weekly"
    if not mode:
        mode = str(sched_cfg.get("mode") or "weekly").strip().lower()
    if mode not in ("weekly", "daily", "hours"):
        raise ValueError(f"不认识的触发方式：{mode}（可选 weekly / daily / hours）")
    return {
        "mode": mode,
        "weekday": weekday or sched_cfg.get("weekday") or "sunday",
        "time": time or sched_cfg.get("time") or "21:00",
        "hours": hours or sched_cfg.get("hours") or 168,
    }


def _trigger_text(sched: dict) -> str:
    """人类可读的触发描述，例如「每周日 21:00」「每 24 小时」。"""
    if sched["mode"] == "hours":
        return f"每 {int(sched['hours'])} 小时"
    hour, minute = _parse_time(sched["time"])
    clock = f"{hour:02d}:{minute:02d}"
    if sched["mode"] == "daily":
        return f"每天 {clock}"
    return f"每周{_WEEKDAY_CN[normalize_weekday(sched['weekday'])]} {clock}"


# ---------------------------------------------------------------- launchctl 调用

def _gui_domain() -> str:
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return f"gui/{uid}"


def _launchctl(args: list[str], timeout: int = 20) -> tuple[bool, str]:
    """调用 launchctl；不抛异常，返回 (是否成功, 输出文字)。"""
    exe = shutil.which("launchctl") or "/bin/launchctl"
    try:
        proc = subprocess.run([exe] + list(args), capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"launchctl 调用失败：{exc}"
    parts = [(proc.stdout or "").strip(), (proc.stderr or "").strip()]
    output = "\n".join(part for part in parts if part)
    return proc.returncode == 0, output


# ---------------------------------------------------------------- Windows 实现

def _powershell_script() -> str:
    return os.path.join(_project_root(), "scripts", "install_task.ps1")


def _call_ps1(params: list[str], *, capture: bool = False) -> tuple[int, str]:
    """调 ``install_task.ps1``；capture=False 时输出直接进控制台（保持老行为）。"""
    script = _powershell_script()
    if not os.path.isfile(script):
        return 127, f"找不到脚本：{script}"
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script] + params
    try:
        if not capture:
            return subprocess.call(cmd), ""
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                              encoding="gbk", errors="ignore")
        output = "\n".join(part.strip() for part in (proc.stdout or "", proc.stderr or "")
                           if part and part.strip())
        return proc.returncode, output
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"启动 PowerShell 失败：{exc}"


def _install_windows(config_path: str | None, task_name: str, *, weekly: bool, weekday,
                     time, daily: bool, hours) -> tuple[bool, str]:
    params = ["-Config", cfgmod.config_path(config_path)]
    # 顺序与原来的 cli.py 完全一致：--hours > --daily > --weekly；
    # 三个都不给时交给 ps1 读配置文件里的 mode。
    if hours:
        params += ["-Hours", str(hours)]
    elif daily:
        params += ["-Daily", "-Time", time or "21:00"]
    elif weekly:
        params += ["-Weekly", "-Weekday", weekday or "Sunday", "-Time", time or "21:00"]
    params += ["-TaskName", task_name]
    code, output = _call_ps1(params)
    if code == 0:
        return True, ""
    # 127 = 脚本不存在，需要把原因告诉用户；其余错误 ps1 自己已经打印过中文原因了。
    return False, (output if code == 127 else "")


def _uninstall_windows(task_name: str) -> tuple[bool, str]:
    code, output = _call_ps1(["-Remove", "-TaskName", task_name])
    if code == 0:
        return True, ""
    return False, (output if code == 127 else "")


def _run_now_windows(task_name: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(["schtasks", "/Run", "/TN", task_name], capture_output=True,
                              text=True, timeout=15, encoding="gbk", errors="ignore")
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"启动失败：{exc}"
    if proc.returncode == 0:
        return True, f"已启动任务 {task_name}，右下角提示窗稍后出现"
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return False, f"启动失败：{output or 'schtasks 返回了非零退出码'}"


def _status_windows(task_name: str) -> dict:
    result = {"installed": False, "name": task_name, "next_run": None,
              "last_result": None, "detail": ""}
    try:
        # /V /FO LIST 的键值一行一个，比默认的表格好解析，也顺带拿到上次结果
        proc = subprocess.run(["schtasks", "/Query", "/TN", task_name, "/V", "/FO", "LIST"],
                              capture_output=True, text=True, timeout=15,
                              encoding="gbk", errors="ignore")
    except (OSError, subprocess.SubprocessError) as exc:
        result["detail"] = f"查询失败：{exc}"
        return result
    if proc.returncode != 0:
        result["detail"] = f"没有名为 {task_name} 的任务（可用 scripts/install_task.ps1 注册）"
        return result

    next_run = ""
    last_result = None
    for line in proc.stdout.splitlines():
        key, sep, value = line.replace("：", ":").partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key in ("Next Run Time", "下次运行时间"):
            next_run = value
        elif key in ("Last Result", "上次结果"):
            # 0x00041303：任务还没跑过
            last_result = "尚未运行过" if value == "267011" else (value or None)
    result["installed"] = True
    result["next_run"] = next_run or None
    result["last_result"] = last_result
    result["detail"] = f"{task_name} 已注册" + (f"，下次 {next_run}" if next_run else "")
    return result


# ---------------------------------------------------------------- macOS 实现

def _install_mac(config: dict | None, config_path: str | None, *, weekly: bool, weekday,
                 time, daily: bool, hours, task_name: str) -> tuple[bool, str]:
    try:
        sched = _resolve_schedule(config, weekly=weekly, daily=daily, hours=hours,
                                  weekday=weekday, time=time)
        label = _label(task_name)
        plist = build_plist(label, command_line(config_path), _project_root(), sched, _log_dir())
    except ValueError as exc:
        return False, str(exc)

    plist_path = _plist_path(task_name)
    try:
        os.makedirs(_launch_agents_dir(), exist_ok=True)
        os.makedirs(_log_dir(), exist_ok=True)
    except OSError as exc:
        return False, f"创建 LaunchAgents / 日志目录失败：{exc}"

    if os.path.isfile(plist_path):
        # 老版本已加载过就先卸掉，失败忽略（本来就没加载的情况很常见）
        _launchctl(["bootout", _gui_domain(), plist_path])

    try:
        with open(plist_path, "wb") as handle:
            plistlib.dump(plist, handle, fmt=plistlib.FMT_XML, sort_keys=False)
    except (OSError, TypeError, ValueError) as exc:
        return False, f"写入 {plist_path} 失败：{exc}"
    get_logger().debug("已写入 LaunchAgent：%s", plist_path)

    ok, output = _launchctl(["bootstrap", _gui_domain(), plist_path])
    if not ok:
        # 老系统（macOS 10.10 之前没有 bootstrap/bootout）回退到 load
        ok, output = _launchctl(["load", "-w", plist_path])
        if not ok:
            return False, (f"launchctl 注册失败：{output or '未知错误'}。"
                           f"可手动检查：launchctl print {_gui_domain()}/{label}")
    return True, f"已注册{_trigger_text(sched)} 自动运行（launchd）"


def _uninstall_mac(task_name: str) -> tuple[bool, str]:
    label = _label(task_name)
    plist_path = _plist_path(task_name)
    if not os.path.isfile(plist_path):
        return True, f"没有找到名为 {label} 的自启任务（可能已经删掉了）"
    _launchctl(["bootout", _gui_domain(), plist_path])       # 失败忽略
    _launchctl(["unload", "-w", plist_path])                 # 老系统兜底，失败忽略
    try:
        os.remove(plist_path)
    except OSError as exc:
        return False, f"删除 {plist_path} 失败：{exc}"
    return True, f"已删除自启任务 {label}（配置文件保留未动）"


def _run_now_mac(task_name: str) -> tuple[bool, str]:
    label = _label(task_name)
    ok, output = _launchctl(["kickstart", "-k", f"{_gui_domain()}/{label}"])
    if ok:
        return True, f"已触发一次运行（{label}）"
    return False, f"触发失败：{output or '任务可能还没注册'}"


def _status_mac(task_name: str) -> dict:
    label = _label(task_name)
    result = {"installed": False, "name": task_name, "next_run": None,
              "last_result": None, "detail": ""}
    plist_path = _plist_path(task_name)
    if not os.path.isfile(plist_path):
        result["detail"] = f"没有注册名为 {label} 的自启任务（可用 scripts/install_task.sh 注册）"
        return result
    ok, output = _launchctl(["print", f"{_gui_domain()}/{label}"])
    if not ok:
        result["detail"] = f"{label} 的 plist 在，但 launchctl 里查不到（未注册），重跑 scripts/install_task.sh 即可"
        return result
    state = ""
    last_result = None
    next_run = None
    for line in output.splitlines():
        text = line.strip()
        if text.startswith("state = "):
            state = text.split("=", 1)[1].strip()
        elif "last exit code" in text:
            last_result = text.split("=", 1)[-1].strip() or None
        elif "next fire date" in text or text.startswith("next run time"):
            stamp = text.split("=", 1)[-1].strip()
            # 形如 "Sunday, October 25, 2026 at 9:00:00 PM (1761…)"，括号里是时间戳，去掉
            next_run = stamp.split(" (", 1)[0] or None
    result["installed"] = True
    result["next_run"] = next_run
    result["last_result"] = last_result
    result["detail"] = f"{label} 已注册（launchd）" + (f"，状态 {state}" if state else "")
    return result


# ---------------------------------------------------------------- 对外 API

def install(config: dict, config_path: str | None, *, weekly: bool = False,
            weekday: str | None = None, time: str | None = None, daily: bool = False,
            hours: int | None = None,
            task_name: str | None = None) -> tuple[bool, str]:
    """注册 / 更新计划任务，返回 (是否成功, 面向用户的说明)。"""
    backend = _backend()
    if not backend:
        return False, UNSUPPORTED_HINT
    name = str(task_name or _task_name_from(config))
    if backend == "schtasks":
        return _install_windows(config_path, name, weekly=weekly, weekday=weekday,
                                time=time, daily=daily, hours=hours)
    return _install_mac(config, config_path, weekly=weekly, weekday=weekday, time=time,
                        daily=daily, hours=hours, task_name=name)


def uninstall(task_name: str | None = None) -> tuple[bool, str]:
    """删除计划任务；任务本来就不存在时也算成功。"""
    backend = _backend()
    if not backend:
        return False, UNSUPPORTED_HINT
    name = str(task_name or _DEFAULT_TASK_NAME)
    if backend == "schtasks":
        return _uninstall_windows(name)
    return _uninstall_mac(name)


def run_now(task_name: str | None = None) -> tuple[bool, str]:
    """立刻运行一次已注册的任务。"""
    backend = _backend()
    if not backend:
        return False, UNSUPPORTED_HINT
    name = str(task_name or _DEFAULT_TASK_NAME)
    if backend == "schtasks":
        return _run_now_windows(name)
    return _run_now_mac(name)


def status(task_name: str | None = None) -> dict:
    """查任务状态，返回 ``{"installed", "name", "next_run", "last_result", "detail"}``。"""
    backend = _backend()
    name = str(task_name or _DEFAULT_TASK_NAME)
    if backend == "schtasks":
        return _status_windows(name)
    if backend == "launchd":
        return _status_mac(name)
    return {"installed": False, "name": name, "next_run": None,
            "last_result": None, "detail": UNSUPPORTED_HINT}
