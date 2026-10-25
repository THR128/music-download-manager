# -*- coding: utf-8 -*-
"""独立进程里显示提示窗。

用法：pythonw.exe -m music_sync.ui.toast_runner <result.json> [timeout]

为什么要独立进程：主流程跑过网络请求后创建窗口会被安全软件的"弹窗拦截"干掉，
新起一个干净的 pythonw 进程就没问题（真机实测）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime


def _heartbeat(message: str) -> None:
    path = os.environ.get("MUSIC_SYNC_TOAST_LOG") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "toast.log")
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("用法: python -m music_sync.ui.toast_runner <result.json> [timeout]")
        return 2
    path = argv[0]
    timeout = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 40
    try:
        with open(path, encoding="utf-8") as fh:
            result = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _heartbeat("读取结果失败：%s" % exc)
        return 1

    _heartbeat("提示窗口启动 pid=%s" % os.getpid())
    try:
        from . import toast
        toast.show(result, timeout)
        _heartbeat("提示窗口正常结束")
    except Exception:                          # noqa: BLE001 - 心跳里要留现场
        import traceback
        _heartbeat("提示窗口异常：" + traceback.format_exc()[-400:])
        return 1
    finally:
        time.sleep(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
