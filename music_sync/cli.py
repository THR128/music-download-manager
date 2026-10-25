# -*- coding: utf-8 -*-
"""命令行入口：python -m music_sync <命令>"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from . import config as cfgmod
from . import platforms
from .util import get_logger, setup_logging


def _load(args) -> dict:
    return cfgmod.load(getattr(args, "config", None))


def _log_path(cfg: dict) -> str:
    return os.path.join(cfgmod.config_dir(), "music-sync.log")


def spawn_toast(result_path: str, timeout: int = 40) -> bool:
    """用独立 pythonw 进程拉起提示窗（避免被安全软件的弹窗拦截干掉）。"""
    log = get_logger()
    exe = sys.executable or ""
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.name != "nt" or not os.path.isfile(pyw):
        pyw = exe
    cmd = [pyw, "-m", "music_sync.ui.toast_runner", result_path, str(timeout)]
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0        # DETACHED | NEW_GROUP
    try:
        proc = subprocess.Popen(cmd, cwd=os.getcwd(), close_fds=True, creationflags=flags)
    except OSError as exc:
        log.warning("拉起提示窗失败：%s", exc)
        return False
    log.info("已拉起结果提示窗口 pid=%s", proc.pid)
    return True


def cmd_sync(args) -> int:
    cfg = _load(args)
    setup_logging(_log_path(cfg), verbose=args.verbose)
    from . import sync as syncer
    from .util import get_logger as _gl

    log = _gl()
    log.info("=== 开始同步（%s）===", "干跑" if args.dry_run else "正式")
    result = syncer.run(cfg, dry_run=args.dry_run)
    path = syncer.write_result(result)
    log.debug("结果已写入 %s", path)

    if result.error:
        log.error("同步失败：%s", result.error)
        return 1

    notify = cfg.get("notify") or {}
    if not args.dry_run and (args.gui or notify.get("toast")):
        spawn_toast(path, int(notify.get("timeout") or 40))
    return 0


def cmd_check(args) -> int:
    cfg = _load(args)
    setup_logging(_log_path(cfg), verbose=args.verbose)
    from . import selfcheck

    checks = selfcheck.run_checks(cfg, getattr(args, "config", None))
    if args.json:
        print(json.dumps([c.__dict__ for c in checks], ensure_ascii=False, indent=2))
    else:
        print(selfcheck.format_text(checks))
    return 1 if any(c.status == selfcheck.FAIL for c in checks) else 0


def cmd_settings(args) -> int:
    cfg = _load(args)
    setup_logging(_log_path(cfg), verbose=args.verbose)
    from .ui import settings

    return settings.run(getattr(args, "config", None))


def cmd_platforms(args) -> int:
    cfg = _load(args)
    setup_logging(_log_path(cfg), verbose=args.verbose)
    primary = platforms.resolve(cfg.get("platform", "netease"))
    for key, (title, can_download) in platforms.PLATFORMS.items():
        mark = "*" if key == primary else " "
        describe = ""
        try:
            adapter = platforms.create(key, cfg.get("platforms", {}).get(key) or {})
            ok, message = adapter.check_login()
            describe = ("已登录" if ok else "未登录") + f"（{message}）"
        except Exception as exc:                     # noqa: BLE001 - 列表要能容错
            describe = f"不可用：{exc}"
        print(f"{mark} {key:<8} {title:<8} 下载={'是' if can_download else '否'}  {describe}")
    print("\n（* 表示当前使用平台）")
    return 0


def cmd_task(args) -> int:
    """把计划任务交给 PowerShell 脚本处理，保持单一实现。"""
    cfg = _load(args)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(root, "scripts", "install_task.ps1")
    if not os.path.isfile(script):
        print(f"找不到脚本：{script}", file=sys.stderr)
        return 1
    params = ["-Config", cfgmod.config_path(getattr(args, "config", None))]
    if args.remove:
        params.append("-Remove")
    elif args.hours:
        params += ["-Hours", str(args.hours)]
    elif args.daily:
        params += ["-Daily", "-Time", args.time or "21:00"]
    elif args.weekly:
        params += ["-Weekly", "-Weekday", args.weekday or "Sunday", "-Time", args.time or "21:00"]
    params += ["-TaskName", (cfg.get("schedule") or {}).get("task_name", "MusicSync")]
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script] + params
    return subprocess.call(cmd)


def cmd_login(args) -> int:
    """获取登录状态：不用手填 cookie。"""
    cfg = _load(args)
    setup_logging(_log_path(cfg), verbose=args.verbose)
    from . import login

    if not args.platform:
        for title, state, detail in login.report(cfg):
            print(f"[{state}] {title} —— {detail}")
        return 0
    ok, message = login.auto(args.platform, cfg, save=not args.no_save,
                             config_path=getattr(args, "config", None))
    print(("✅ " if ok else "❌ ") + message)
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="music-sync",
        description="音乐下载自动化管理：把音乐平台的「我喜欢」补齐到本地曲库（多平台 · 可定时 · 可换源）")
    parser.add_argument("--config", help="配置文件路径（默认 ~/.music-sync/config.json）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--version", action="version", version="music-sync 1.0.0")
    sub = parser.add_subparsers(dest="command")

    p_login = sub.add_parser("login", help="获取登录状态（自动读取已装客户端/浏览器，不用手填 cookie）")
    p_login.add_argument("platform", nargs="?", help="平台：netease / qqmusic / kugou / soda，留空=全部")
    p_login.add_argument("--no-save", action="store_true", help="只检测，不写入配置文件")
    p_login.set_defaults(func=cmd_login)

    p_sync = sub.add_parser("sync", help="执行一次同步")
    p_sync.add_argument("--dry-run", action="store_true", help="只对账，不下载")
    p_sync.add_argument("--gui", action="store_true", help="结束后弹右下角提示窗（计划任务用）")
    p_sync.set_defaults(func=cmd_sync)

    p_check = sub.add_parser("check", help="环境自检")
    p_check.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_check.set_defaults(func=cmd_check)

    p_set = sub.add_parser("settings", help="打开设置界面")
    p_set.set_defaults(func=cmd_settings)

    p_pl = sub.add_parser("platforms", help="列出平台与登录态")
    p_pl.set_defaults(func=cmd_platforms)

    p_task = sub.add_parser("task", help="注册/更新 Windows 计划任务")
    p_task.add_argument("--weekly", action="store_true", help="每周执行")
    p_task.add_argument("--daily", action="store_true", help="每天执行")
    p_task.add_argument("--hours", type=int, help="每隔 N 小时执行")
    p_task.add_argument("--weekday", help="周几（Monday..Sunday）")
    p_task.add_argument("--time", help="时间 HH:MM")
    p_task.add_argument("--remove", action="store_true", help="删除计划任务")
    p_task.set_defaults(func=cmd_task)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args.command = "sync"
        args.func = cmd_sync
        args.dry_run = False
        args.gui = False
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
