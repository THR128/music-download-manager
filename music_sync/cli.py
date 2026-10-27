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
    """注册 / 删除 / 立即运行计划任务；Windows 用 schtasks，macOS 用 launchd。

    具体实现都在 :mod:`music_sync.schedule` 里，这里只负责选动作和打印结果。
    """
    cfg = _load(args)
    from . import schedule

    name = (getattr(args, "task_name", None)
            or (cfg.get("schedule") or {}).get("task_name") or "MusicSync")
    config_path = cfgmod.config_path(getattr(args, "config", None))
    if args.remove:
        ok, message = schedule.uninstall(task_name=name)
    elif getattr(args, "run_now", False):
        ok, message = schedule.run_now(task_name=name)
    else:
        ok, message = schedule.install(
            cfg, config_path,
            weekly=args.weekly, weekday=args.weekday, time=args.time,
            daily=args.daily, hours=args.hours, task_name=name)
    if message:
        # Windows 下 install_task.ps1 自己会打印详细步骤，成功时 message 为空，
        # 这里就不会多出一行——保持原来的输出。
        print(("✅ " if ok else "❌ ") + message)
    return 0 if ok else 1


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


def cmd_unlock(args) -> int:
    """把加密音频解锁成普通音频（.ncm / .qmc* / .kgm …）。"""
    cfg = _load(args)
    setup_logging(_log_path(cfg), verbose=args.verbose)
    from . import unlock as unlock_mod
    from .util import human_size

    library = cfg.get("library_dir") or ""
    targets = list(getattr(args, "dirs", []) or []) + list(getattr(args, "dirs_extra", []) or [])
    if not targets:
        client_dir = cfg.get("client_download_dir") or ""
        if client_dir:
            targets.append(client_dir)
        if library:
            targets.append(library)
    targets = [p for p in targets if p]
    if not targets:
        print("❌ 没有可扫描的目录：请用 --dir 指定，或在配置里填好下载目录/曲库目录")
        return 1

    dest = args.dest or library or ""
    if not dest:
        print("❌ 没有输出目录：请用 --dest 指定，或在配置里填好曲库目录")
        return 1

    total_ok = total_fail = 0
    for directory in targets:
        if not os.path.isdir(directory):
            print(f"跳过（目录不存在）：{directory}")
            continue
        can, hints = unlock_mod.scan_encrypted(directory, recursive=not args.no_recursive)
        print(f"\n{directory}：可解锁 {len(can)} 个，只支持提示的 {len(hints)} 个")
        for path in hints:
            print(f"  · 暂不支持：{os.path.basename(path)}")
        for path in can:
            name = os.path.basename(path)
            try:
                done = unlock_mod.unlock(path, dest_dir=dest)
            except unlock_mod.UnlockError as exc:
                total_fail += 1
                print(f"  ✗ {name}：{exc}")
                continue
            total_ok += 1
            print(f"  ✓ {name} → {os.path.basename(done.path)}"
                  f"（{done.fmt}，{human_size(os.path.getsize(done.path))}）")
            if args.remove_source:
                try:
                    os.remove(path)
                except OSError as exc:
                    print(f"    （删除原文件失败：{exc}）")

    print(f"\n共解锁 {total_ok} 个，失败 {total_fail} 个，输出目录：{dest}")
    return 0 if total_fail == 0 else 1


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

    p_unlock = sub.add_parser("unlock", help="把客户端下载的加密音频解锁成普通 mp3/flac")
    p_unlock.add_argument("dirs", nargs="*", help="要扫描的目录（默认：配置里的下载目录 + 曲库目录）")
    p_unlock.add_argument("--dir", dest="dirs_extra", action="append", default=[],
                          help="额外扫描目录，可重复")
    p_unlock.add_argument("--dest", help="输出目录（默认：配置里的曲库目录）")
    p_unlock.add_argument("--remove-source", action="store_true", help="解锁成功后删掉原加密文件")
    p_unlock.add_argument("--no-recursive", action="store_true", help="不递归子目录")
    p_unlock.set_defaults(func=cmd_unlock)

    p_pl = sub.add_parser("platforms", help="列出平台与登录态")
    p_pl.set_defaults(func=cmd_platforms)

    p_task = sub.add_parser("task", help="注册/更新计划任务（Windows 用 schtasks，macOS 用 launchd）")
    p_task.add_argument("--weekly", action="store_true", help="每周执行")
    p_task.add_argument("--daily", action="store_true", help="每天执行")
    p_task.add_argument("--hours", type=int, help="每隔 N 小时执行")
    p_task.add_argument("--weekday", help="周几（Monday..Sunday）")
    p_task.add_argument("--time", help="时间 HH:MM")
    p_task.add_argument("--remove", action="store_true", help="删除计划任务")
    p_task.add_argument("--run-now", action="store_true", help="立即运行一次已注册的任务")
    p_task.add_argument("--task-name", help="任务名（默认读配置里的 schedule.task_name）")
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
